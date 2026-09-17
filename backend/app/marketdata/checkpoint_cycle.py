"""The live refresh -> commit -> capture choreography (seam doc §3.3).

    refresh market data          network, OUTSIDE any DB transaction
        |
        v
    persist immutable PropQuotes, COMMIT
        |
        v
    capture checkpoint           DB reads only, at the real captured_at

Three properties this exists to guarantee, none of which a caller should
have to remember:

1. **No provider HTTP inside `capture_checkpoint`.** A network call inside
   a capture would hold a transaction open across an unbounded wait, and a
   provider timeout would abort a capture that had already written half a
   slate's snapshots. `capture_checkpoint` reads the database and nothing
   else; there is an import guard asserting it cannot reach a provider.

2. **The capture clock is read AFTER the refresh finishes.** Reading it
   first would make every snapshot claim a `taken_at` earlier than the
   quotes it just consumed -- the exact bug the 4A.2 acceptance run hit,
   where a snapshot could not see its own fresh quotes.

3. **A failed refresh does not abort the capture.** That is the whole
   point of the tolerance. A capture that runs on last-known observations
   and marks them stale is informative; a capture that does not happen is
   a hole in the research record. The gate decides whether those
   observations are usable, and the report says plainly that the refresh
   failed.

This is a callable job, not a scheduler. Something else decides when to
call it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import select

from app.db.models.forecast_lab import EvidenceSnapshot
from app.db.models.markets import Game, MarketSnapshot
from app.db.session import session_scope
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.forecast_lab.quote_selection import validate_max_observation_age


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What a market-data refresh reports back.

    Deliberately carries no session, no provider object and no raw payload:
    the refresh owns its own transactions and has already committed by the
    time it returns this. A cycle must not be able to reach back into a
    refresh's database work.
    """

    ok: bool
    started_at: datetime
    completed_at: datetime
    quotes_written: int = 0
    quotes_deduplicated: int = 0
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()


class RefreshCallable(Protocol):
    def __call__(self) -> RefreshOutcome: ...


@dataclass
class BookFreshness:
    sportsbook: str
    latest_as_of_at: datetime
    observation_age_seconds: float
    included: bool
    is_canonical: bool


@dataclass
class CycleReport:
    """Everything needed to judge one capture, with the two clocks kept
    separate. Merging them is the methodology error §3.2 exists to
    prevent: a late scheduler and a stale market are different problems
    with different fixes."""

    game_id: uuid.UUID
    checkpoint_type: str
    max_observation_age_seconds: int | None

    refresh_attempted: bool = False
    refresh_ok: bool = False
    refresh_started_at: datetime | None = None
    refresh_completed_at: datetime | None = None
    refresh_error: str | None = None
    quotes_written: int = 0

    checkpoint_status: str = "NOT_RUN"
    captured_at: datetime | None = None
    target_time: datetime | None = None

    markets_captured: int = 0
    valid_baselines: int = 0
    canonical_stale: int = 0
    books_observed: int = 0
    stale_books_excluded: int = 0
    observation_ages_seconds: list[float] = field(default_factory=list)
    latest_by_book: dict[str, BookFreshness] = field(default_factory=dict)

    @property
    def scheduler_offset_seconds(self) -> float | None:
        """`captured_at - target_time`. Positive means the capture fired
        late. This is about the SCHEDULER, never about the market, and is
        never an input to the freshness gate."""

        if self.captured_at is None or self.target_time is None:
            return None
        return (self.captured_at - self.target_time).total_seconds()

    @property
    def max_observation_age_observed(self) -> float | None:
        return max(self.observation_ages_seconds) if self.observation_ages_seconds else None

    @property
    def refresh_to_capture_seconds(self) -> float | None:
        """How long after the refresh finished the capture clock was read.
        In a healthy cycle this is small and so are the observation ages."""

        if self.refresh_completed_at is None or self.captured_at is None:
            return None
        return (self.captured_at - self.refresh_completed_at).total_seconds()


def run_checkpoint_cycle(
    *,
    game_id: uuid.UUID,
    checkpoint_type: str,
    windows_config: dict,
    canonical_sportsbook: str,
    market_data_provider: str,
    refresh: RefreshCallable | None = None,
    max_observation_age_seconds: int | None = None,
    devig_method: str = "PROPORTIONAL_V1",
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CycleReport:
    """One scheduler-ready capture cycle for one game.

    `refresh` is injected rather than constructed here so that the
    choreography can be tested against a failing refresh without a network
    and without spending provider credits. In production it is the real
    ingestion pass; it must commit before returning.
    """

    # Validated up front: a bad tolerance should stop the cycle before it
    # spends a provider call, not after.
    max_age = validate_max_observation_age(max_observation_age_seconds)

    report = CycleReport(
        game_id=game_id,
        checkpoint_type=checkpoint_type,
        max_observation_age_seconds=max_age,
    )

    # --- 1. refresh. Network, no open transaction, no session held. ---
    if refresh is not None:
        report.refresh_attempted = True
        outcome = refresh()
        report.refresh_ok = outcome.ok
        report.refresh_started_at = outcome.started_at
        report.refresh_completed_at = outcome.completed_at
        report.refresh_error = outcome.error
        report.quotes_written = outcome.quotes_written

    # --- 2. the capture clock, read AFTER the refresh settled ---------
    captured_at = now_fn()

    # --- 3. capture. Database reads only. -----------------------------
    with session_scope() as session:
        game = session.get(Game, game_id)
        if game is None:
            raise LookupError(f"game {game_id} not found")
        run = capture_checkpoint(
            session,
            game=game,
            checkpoint_type=checkpoint_type,
            windows_config=windows_config,
            now=captured_at,
            canonical_sportsbook=canonical_sportsbook,
            devig_method=devig_method,
            market_data_provider=market_data_provider,
            max_observation_age_seconds=max_age,
        )
        report.checkpoint_status = run.status
        report.captured_at = run.captured_at
        report.target_time = run.target_time
        run_id = run.id

    # --- 4. read back what the capture decided ------------------------
    if report.checkpoint_status == "CAPTURED":
        _summarize(report, run_id=run_id, canonical_sportsbook=canonical_sportsbook)
    return report


def _summarize(report: CycleReport, *, run_id: uuid.UUID, canonical_sportsbook: str) -> None:
    """Read-back only. Every number here comes from what was persisted, not
    from what the cycle intended to persist — a report built from
    intentions cannot catch a capture that silently did something else."""

    with session_scope() as session:
        # Reached through THIS run's evidence rows, not through
        # "latest snapshot per market". A market can carry snapshots from
        # several checkpoints, and summarizing whichever is newest would
        # quietly report another capture's numbers as this one's.
        snapshots = session.execute(
            select(MarketSnapshot)
            .join(EvidenceSnapshot, EvidenceSnapshot.market_snapshot_id == MarketSnapshot.id)
            .where(EvidenceSnapshot.checkpoint_run_id == run_id)
            .order_by(MarketSnapshot.market_id)
        ).scalars().all()
        for snapshot in snapshots:
            report.markets_captured += 1
            report.valid_baselines += int(snapshot.is_valid_canonical_baseline)
            report.canonical_stale += int(snapshot.canonical_quote_stale)
            report.books_observed += snapshot.books_observed
            report.stale_books_excluded += snapshot.stale_books_excluded
            _collect_book_freshness(report, snapshot, canonical_sportsbook)


def _collect_book_freshness(
    report: CycleReport, snapshot: MarketSnapshot, canonical_sportsbook: str
) -> None:
    record = snapshot.selected_quotes or {}
    for bucket, included in (("included", True), ("excluded_stale", False)):
        for entry in record.get(bucket, []):
            age = float(entry["observation_age_seconds"])
            report.observation_ages_seconds.append(age)
            as_of = datetime.fromisoformat(entry["as_of_at"])
            book = entry["sportsbook"]
            current = report.latest_by_book.get(book)
            # Across a game's markets one book can appear many times; keep
            # its freshest observation, since "is this book still with us"
            # is a per-book question, not a per-market one.
            if current is None or as_of > current.latest_as_of_at:
                report.latest_by_book[book] = BookFreshness(
                    sportsbook=book,
                    latest_as_of_at=as_of,
                    observation_age_seconds=age,
                    included=included,
                    is_canonical=book == canonical_sportsbook,
                )


def render(report: CycleReport) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 72)
    add(f"CHECKPOINT CYCLE — {report.checkpoint_type}  game {report.game_id}")
    add("=" * 72)
    add("--- refresh (network, outside any transaction) ----------------------")
    if not report.refresh_attempted:
        add("  no refresh supplied (capture ran against already-persisted quotes)")
    else:
        add(f"  status:        {'OK' if report.refresh_ok else 'FAILED'}")
        add(f"  started_at:    {report.refresh_started_at}")
        add(f"  completed_at:  {report.refresh_completed_at}")
        add(f"  quotes written:{report.quotes_written}")
        if report.refresh_error:
            add(f"  error:         {report.refresh_error}")
    add("")
    add("--- the two clocks, kept apart --------------------------------------")
    add(f"  target_time:      {report.target_time}   (scheduling intent)")
    add(f"  captured_at:      {report.captured_at}   (what the gate measures against)")
    add(f"  scheduler_offset: {report.scheduler_offset_seconds}s")
    add(f"  refresh->capture: {report.refresh_to_capture_seconds}s")
    add("")
    add("--- capture ---------------------------------------------------------")
    add(f"  status:               {report.checkpoint_status}")
    add(f"  tolerance in force:   {report.max_observation_age_seconds}")
    add(f"  markets captured:     {report.markets_captured}")
    add(f"  valid baselines:      {report.valid_baselines}")
    add(f"  canonical STALE:      {report.canonical_stale}")
    add(f"  books observed:       {report.books_observed}")
    add(f"  stale books excluded: {report.stale_books_excluded}")
    add(f"  max observation age:  {report.max_observation_age_observed}s")
    add("")
    add("--- latest observation per book -------------------------------------")
    for book in sorted(report.latest_by_book):
        f = report.latest_by_book[book]
        mark = "CANONICAL" if f.is_canonical else "         "
        state = "kept " if f.included else "STALE"
        add(f"  {mark} {state} {book:14} as_of={f.latest_as_of_at}  age={f.observation_age_seconds:.1f}s")
    add("")
    add("=" * 72)
    return "\n".join(out)
