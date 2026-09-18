"""The live preflight -> refresh -> commit -> capture choreography
(seam doc §3.3).

    preflight                    DB reads only. May this checkpoint work?
        |  ELIGIBLE only
        v
    refresh market data          network, OUTSIDE any DB transaction,
        |                        bounded explicit retries
        v
    persist immutable PropQuotes, COMMIT
        |
        v
    capture checkpoint           DB reads only, at the real captured_at

Four properties this exists to guarantee, none of which a caller should
have to remember:

1. **A paid refresh happens only when the checkpoint can actually use it.**
   The preflight runs first and is read-only. An already-CAPTURED
   checkpoint is immutable, so new quotes could never reach it; a
   too-early or expired one will not capture at all. Refreshing in any of
   those cases buys nothing and costs credits — and a scheduler that
   re-ticks would pay again every time. This is a cost-integrity
   invariant, not an optimization. (Phase 4A.4 shipped this the wrong way
   round: it refreshed first and discovered the disposition afterwards.)

2. **No provider HTTP inside `capture_checkpoint`.** A network call inside
   a capture would hold a transaction open across an unbounded wait, and a
   provider timeout would abort a capture that had already written half a
   slate's snapshots. There is an import guard asserting the capture path
   cannot reach a provider.

3. **The capture clock is read AFTER the final refresh attempt settles.**
   Reading it first would make every snapshot claim a `taken_at` earlier
   than the quotes it just consumed — the exact bug the 4A.2 acceptance
   run hit.

4. **Capture is irreversible, so retries happen BEFORE it (MODEL B).**
   `capture_checkpoint` is idempotent once CAPTURED, so "retry by calling
   the cycle again" cannot work: the first attempt has already consumed
   the checkpoint. Bounded, explicit, individually-audited attempts run
   inside this one call; when they are exhausted the capture proceeds on
   last-known observations and the freshness gate labels them. A missing
   checkpoint is a hole in the research record; a stale-but-labelled one
   is information.

This is a callable job, not a scheduler, and there is no continuous
poller. Something else decides when to call it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import select

from app.db.models.forecast_lab import EvidenceSnapshot
from app.db.models.markets import Game, MarketSnapshot
from app.db.models.season import SeasonRules
from app.db.repositories.checkpoint_repository import CheckpointRepository
from app.db.session import session_scope
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.forecast_lab.checkpoint_window import (
    CheckpointDisposition,
    CheckpointWindow,
    compute_window,
    disposition,
)
from app.forecast_lab.quote_selection import validate_max_observation_age

# Categories where trying again can plausibly succeed. Everything else is
# excluded deliberately:
#
#   AUTHENTICATION_ERROR      a bad key stays bad
#   QUOTA_EXHAUSTED           retrying is guaranteed to fail
#   QUOTA_RESERVE_EXHAUSTED   same, and the reserve exists to stop exactly this
#   MALFORMED_RESPONSE        the provider ANSWERED and we were billed; the
#                             same request yields the same unusable payload,
#                             so a retry pays again for the same garbage
RETRYABLE_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {"TIMEOUT", "PROVIDER_UNAVAILABLE", "RATE_LIMITED", "UNKNOWN_PROVIDER_ERROR"}
)


class CapturePolicyError(ValueError):
    """The capture policy is unusable. Raised before anything is spent."""


@dataclass(frozen=True, slots=True)
class RefreshRetryPolicy:
    """How many times a failed refresh may be retried before the capture.

    `max_attempts=1` with no backoff is MODEL A: one shot, then capture
    loud. Anything more is MODEL B.

    Retries are explicit and individually audited here rather than hidden
    inside an adapter: each attempt is its own provider call with its own
    `ProviderCall` row, because "how many times did we ask, and what did
    each attempt say" is research provenance, not an implementation
    detail. An adapter-level retry would make three billed calls look like
    one.
    """

    max_attempts: int = 1
    backoff_seconds: tuple[float, ...] = ()
    # Stop retrying if the next attempt plus a capture would not comfortably
    # fit before window_end. Retries must never consume the window they
    # exist to protect.
    window_guard_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise CapturePolicyError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if any(d < 0 for d in self.backoff_seconds):
            raise CapturePolicyError("backoff_seconds must all be >= 0")
        if len(self.backoff_seconds) > self.max_attempts - 1:
            raise CapturePolicyError(
                f"{len(self.backoff_seconds)} backoff delays for {self.max_attempts} "
                "attempts; there are only max_attempts-1 gaps"
            )
        if self.window_guard_seconds < 0:
            raise CapturePolicyError("window_guard_seconds must be >= 0")

    def delay_before(self, attempt_number: int) -> float:
        """Seconds to wait before attempt N (1-indexed). Attempt 1 is
        immediate; a missing entry reuses the last delay."""

        if attempt_number <= 1 or not self.backoff_seconds:
            return 0.0
        index = min(attempt_number - 2, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index]

    @property
    def worst_case_seconds(self) -> float:
        return sum(self.delay_before(n) for n in range(2, self.max_attempts + 1))

    def as_record(self) -> dict:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": list(self.backoff_seconds),
            "window_guard_seconds": self.window_guard_seconds,
        }

    @classmethod
    def from_record(cls, record: dict | None) -> "RefreshRetryPolicy":
        if not record:
            return SINGLE_ATTEMPT
        return cls(
            max_attempts=int(record["max_attempts"]),
            backoff_seconds=tuple(float(d) for d in record.get("backoff_seconds", ())),
            window_guard_seconds=float(record.get("window_guard_seconds", 60.0)),
        )


SINGLE_ATTEMPT = RefreshRetryPolicy(max_attempts=1)
"""MODEL A. One attempt, then capture whatever we hold."""


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """Everything frozen about how a capture behaves.

    `rules_version` is the provenance: a policy resolved from a
    `SeasonRules` row carries that row's version, and one constructed
    directly does not. An official capture is therefore auditable after
    the fact — the report says whether the rules were frozen or injected,
    so "an operator picked a threshold by hand for this run" cannot look
    the same as "the season's rules said so".
    """

    canonical_sportsbook: str
    market_data_provider: str
    checkpoint_windows: dict
    devig_method: str = "PROPORTIONAL_V1"
    max_observation_age_seconds: int | None = None
    retry: RefreshRetryPolicy = SINGLE_ATTEMPT
    rules_version: str | None = None

    def __post_init__(self) -> None:
        validate_max_observation_age(self.max_observation_age_seconds)
        if not self.checkpoint_windows:
            raise CapturePolicyError("checkpoint_windows must not be empty")

    @property
    def is_official(self) -> bool:
        """True only when every value came from a frozen SeasonRules row."""

        return self.rules_version is not None

    @classmethod
    def from_season_rules(cls, session, *, season_id: uuid.UUID) -> "CapturePolicy":
        """The ONLY constructor an official capture may use.

        An operator running a real checkpoint does not get to choose the
        tolerance or the retry budget: both materially change which market
        state may reach a checkpoint, so both are part of the season's
        frozen rules and a change is a rules amendment, never a flag.
        """

        rules = session.execute(
            select(SeasonRules)
            .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
            .order_by(SeasonRules.effective_from.desc())
        ).scalars().first()
        if rules is None:
            raise CapturePolicyError(f"season {season_id} has no active SeasonRules")
        return cls(
            canonical_sportsbook=rules.canonical_sportsbook,
            market_data_provider=rules.market_data_provider,
            checkpoint_windows=dict(rules.checkpoint_windows),
            devig_method=rules.devig_method,
            max_observation_age_seconds=rules.max_observation_age_seconds,
            retry=RefreshRetryPolicy.from_record(rules.refresh_retry_policy),
            rules_version=rules.rules_version,
        )


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What ONE market-data refresh attempt reports back.

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
    error_category: str | None = None
    provider_calls: int = 1
    quota_cost: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def retryable(self) -> bool:
        if self.ok:
            return False
        return self.error_category in RETRYABLE_ERROR_CATEGORIES


class RefreshCallable(Protocol):
    def __call__(self) -> RefreshOutcome: ...


@dataclass
class RefreshAttempt:
    attempt: int
    outcome: RefreshOutcome
    waited_seconds: float


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
    rules_version: str | None = None

    disposition: CheckpointDisposition | None = None
    refresh_skipped_reason: str | None = None
    attempts: list[RefreshAttempt] = field(default_factory=list)

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

    # --- refresh accounting -------------------------------------------

    @property
    def refresh_attempted(self) -> bool:
        return bool(self.attempts)

    @property
    def refresh_ok(self) -> bool:
        return any(a.outcome.ok for a in self.attempts)

    @property
    def provider_calls_spent(self) -> int:
        """What this cycle cost. Zero is the required answer for a
        checkpoint that could not have used the data."""

        return sum(a.outcome.provider_calls for a in self.attempts)

    @property
    def quota_cost(self) -> int:
        return sum(a.outcome.quota_cost for a in self.attempts)

    @property
    def refresh_started_at(self) -> datetime | None:
        return self.attempts[0].outcome.started_at if self.attempts else None

    @property
    def refresh_completed_at(self) -> datetime | None:
        return self.attempts[-1].outcome.completed_at if self.attempts else None

    @property
    def refresh_error(self) -> str | None:
        """The LAST attempt's error, or None if any attempt succeeded."""

        if not self.attempts or self.refresh_ok:
            return None
        return self.attempts[-1].outcome.error

    @property
    def quotes_written(self) -> int:
        return sum(a.outcome.quotes_written for a in self.attempts)

    # --- the two clocks, never merged ---------------------------------

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
        """How long after the last refresh attempt finished the capture
        clock was read. In a healthy cycle this is small and so are the
        observation ages."""

        if self.refresh_completed_at is None or self.captured_at is None:
            return None
        return (self.captured_at - self.refresh_completed_at).total_seconds()


@dataclass(frozen=True, slots=True)
class CheckpointPreflight:
    disposition: CheckpointDisposition
    window: CheckpointWindow
    existing_status: str | None


def preflight_checkpoint(
    session, *, game: Game, checkpoint_type: str, windows_config: dict, now: datetime
) -> CheckpointPreflight:
    """Read-only. Decides whether a paid refresh is justified.

    Writes nothing — in particular it does NOT create the PENDING
    `CheckpointRun` row. Creating state as a side effect of asking a
    question would mean a too-early poll silently changed the record it
    was only supposed to read. `capture_checkpoint` still creates and
    advances the row afterwards exactly as before; this only gates the
    provider call.
    """

    run = CheckpointRepository(session).get(game.id, checkpoint_type)
    window = (
        CheckpointWindow(run.window_start, run.window_end, run.target_time)
        if run is not None
        else compute_window(game.kickoff_at, checkpoint_type, windows_config)
    )
    return CheckpointPreflight(
        disposition=disposition(
            status=run.status if run is not None else None, window=window, now=now
        ),
        window=window,
        existing_status=run.status if run is not None else None,
    )


def run_checkpoint_cycle(
    *,
    game_id: uuid.UUID,
    checkpoint_type: str,
    policy: CapturePolicy,
    refresh: RefreshCallable | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep_fn: Callable[[float], None] | None = None,
) -> CycleReport:
    """One capture cycle for one game.

    `refresh` is injected rather than constructed here so the choreography
    can be tested against a failing refresh without a network and without
    spending provider credits. In production it is the real ingestion
    pass; it must commit before returning.
    """

    report = CycleReport(
        game_id=game_id,
        checkpoint_type=checkpoint_type,
        max_observation_age_seconds=policy.max_observation_age_seconds,
        rules_version=policy.rules_version,
    )

    # --- 1. preflight. Read-only, free. -------------------------------
    with session_scope() as session:
        game = session.get(Game, game_id)
        if game is None:
            raise LookupError(f"game {game_id} not found")
        pre = preflight_checkpoint(
            session,
            game=game,
            checkpoint_type=checkpoint_type,
            windows_config=policy.checkpoint_windows,
            now=now_fn(),
        )
    report.disposition = pre.disposition

    # --- 2. refresh, ONLY if the checkpoint can use it ----------------
    if refresh is None:
        report.refresh_skipped_reason = "no refresh supplied"
    elif not pre.disposition.may_refresh:
        report.refresh_skipped_reason = str(pre.disposition)
    else:
        _run_refresh_attempts(
            report, refresh=refresh, policy=policy, window=pre.window,
            now_fn=now_fn, sleep_fn=sleep_fn,
        )

    # --- 3. the capture clock, read AFTER the last attempt settled ----
    captured_at = now_fn()

    # --- 4. capture. Database reads only, and always safe to call:
    #        idempotent on CAPTURED, and it owns the PENDING/MISSED
    #        bookkeeping the preflight deliberately did not do.
    with session_scope() as session:
        game = session.get(Game, game_id)
        run = capture_checkpoint(
            session,
            game=game,
            checkpoint_type=checkpoint_type,
            windows_config=policy.checkpoint_windows,
            now=captured_at,
            canonical_sportsbook=policy.canonical_sportsbook,
            devig_method=policy.devig_method,
            market_data_provider=policy.market_data_provider,
            max_observation_age_seconds=policy.max_observation_age_seconds,
        )
        report.checkpoint_status = run.status
        report.captured_at = run.captured_at
        report.target_time = run.target_time
        run_id = run.id

    # --- 5. read back what the capture decided ------------------------
    if report.checkpoint_status == "CAPTURED":
        _summarize(report, run_id=run_id, canonical_sportsbook=policy.canonical_sportsbook)
    return report


def _run_refresh_attempts(
    report: CycleReport,
    *,
    refresh: RefreshCallable,
    policy: CapturePolicy,
    window: CheckpointWindow,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None] | None,
) -> None:
    """MODEL B: bounded, explicit, individually-audited attempts, all of
    them before the irreversible capture."""

    import time

    sleep = sleep_fn if sleep_fn is not None else time.sleep

    for attempt in range(1, policy.retry.max_attempts + 1):
        waited = 0.0
        if attempt > 1:
            delay = policy.retry.delay_before(attempt)
            # A retry must never consume the window it exists to protect.
            # If waiting would leave too little room to capture, stop and
            # let the capture proceed on what we hold.
            remaining = window.seconds_remaining(now_fn())
            if remaining - delay < policy.retry.window_guard_seconds:
                report.refresh_skipped_reason = (
                    f"retry {attempt} would leave {remaining - delay:.0f}s of the "
                    f"window, under the {policy.retry.window_guard_seconds:.0f}s guard"
                )
                return
            sleep(delay)
            waited = delay

        outcome = refresh()
        report.attempts.append(RefreshAttempt(attempt=attempt, outcome=outcome, waited_seconds=waited))

        if outcome.ok:
            return
        if not outcome.retryable:
            report.refresh_skipped_reason = (
                f"{outcome.error_category} is not retryable; a retry cannot succeed "
                "and would be billed"
            )
            return

    report.refresh_skipped_reason = f"retry budget of {policy.retry.max_attempts} attempts exhausted"


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
    add("--- preflight (read only, free) ------------------------------------")
    add(f"  disposition:      {report.disposition}")
    add(f"  policy source:    {report.rules_version or 'INJECTED (not frozen rules)'}")
    add("")
    add("--- refresh (network, outside any transaction) ----------------------")
    add(f"  provider calls:   {report.provider_calls_spent}")
    add(f"  quota cost:       {report.quota_cost}")
    if report.refresh_skipped_reason:
        add(f"  stopped because:  {report.refresh_skipped_reason}")
    for a in report.attempts:
        state = "OK" if a.outcome.ok else f"FAILED {a.outcome.error_category}"
        add(
            f"    attempt {a.attempt}: waited {a.waited_seconds:.0f}s  {state}  "
            f"quotes={a.outcome.quotes_written}"
        )
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
