"""Threshold calibration preview — READ ONLY (seam doc §3.4).

Answers "what WOULD each candidate tolerance have selected, at this
capture time, against the quotes we already hold" without writing a
`MarketSnapshot`, a `CheckpointRun` or an `EvidenceSnapshot`.

Why this exists rather than just capturing with a candidate threshold:
`capture_checkpoint` is deliberately idempotent once a run reaches
CAPTURED. Capturing FINAL on the durable DET @ BUF game to try a number
would permanently freeze experimental artifacts onto a real game, and
there is no second attempt. Calibration therefore never captures.

It does NOT re-implement the rule. Every candidate is scored through
`MarketSnapshotService.selection_plan`, the same call `build_snapshot`
makes, so a preview and a real capture cannot disagree about what would
have been selected. A preview built on its own copy of the rule would be
evidence about the copy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.repositories.market_repository import MarketRepository
from app.forecast_lab.market_snapshot_service import MarketSnapshotService


@dataclass
class CandidateResult:
    """What one tolerance would have produced across a game's markets."""

    max_observation_age_seconds: int | None
    markets: int = 0
    valid_baselines: int = 0
    canonical_stale: int = 0
    canonical_absent: int = 0
    books_observed: int = 0
    books_included: int = 0
    stale_books_excluded: int = 0
    # Every per-book observation age seen, so a reader can judge the
    # distribution rather than only the verdict a threshold produced.
    observation_ages_seconds: list[float] = field(default_factory=list)
    stale_books: dict[str, int] = field(default_factory=dict)

    @property
    def baseline_loss_rate(self) -> float:
        """Fraction of markets this tolerance would cost a canonical
        baseline. The number that actually matters: a tolerance is only
        worth tightening until it starts blinding the slate."""

        return 0.0 if not self.markets else 1 - (self.valid_baselines / self.markets)

    @property
    def max_age(self) -> float | None:
        return max(self.observation_ages_seconds) if self.observation_ages_seconds else None

    @property
    def median_age(self) -> float | None:
        if not self.observation_ages_seconds:
            return None
        ordered = sorted(self.observation_ages_seconds)
        return ordered[len(ordered) // 2]


@dataclass
class CalibrationReport:
    game_id: uuid.UUID
    taken_at: datetime
    canonical_sportsbook: str
    market_data_provider: str
    candidates: list[CandidateResult] = field(default_factory=list)

    def tightest_without_baseline_loss(self) -> CandidateResult | None:
        """The strictest candidate that still leaves every market a valid
        canonical baseline.

        A recommendation input, NOT an answer. The strictest threshold this
        particular capture tolerated is a property of this capture's luck,
        not of the operational grace period we owe a failed refresh. It is
        reported so a human can see where the cliff is.
        """

        gated = [
            c for c in self.candidates
            if c.max_observation_age_seconds is not None and c.markets and c.valid_baselines == c.markets
        ]
        return min(gated, key=lambda c: c.max_observation_age_seconds) if gated else None


def preview_thresholds(
    session: Session,
    *,
    game_id: uuid.UUID,
    taken_at: datetime,
    canonical_sportsbook: str,
    market_data_provider: str,
    candidates: list[int | None],
) -> CalibrationReport:
    """Score each candidate tolerance against already-persisted quotes.

    Writes nothing. The session is used for SELECTs only, which
    `test_calibration_is_read_only` asserts structurally rather than
    trusting this docstring.
    """

    repo = MarketRepository(session)
    markets = repo.markets_for_game(game_id)

    report = CalibrationReport(
        game_id=game_id,
        taken_at=taken_at,
        canonical_sportsbook=canonical_sportsbook,
        market_data_provider=market_data_provider,
    )

    for candidate in candidates:
        service = MarketSnapshotService(
            session,
            market_data_provider=market_data_provider,
            max_observation_age_seconds=candidate,
        )
        result = CandidateResult(max_observation_age_seconds=candidate)
        for market in markets:
            plan = service.selection_plan(
                market_id=market.id,
                canonical_sportsbook=canonical_sportsbook,
                taken_at=taken_at,
            )
            if plan.books_observed == 0:
                # No quote at all for this market at `taken_at`. Not a
                # freshness outcome -- counting it as one would make every
                # candidate look equally bad on a market no threshold
                # could have saved.
                continue
            result.markets += 1
            result.books_observed += plan.books_observed
            result.books_included += len(plan.included)
            result.stale_books_excluded += plan.stale_books_excluded
            if plan.canonical is not None:
                result.valid_baselines += 1
            elif plan.canonical_quote_stale:
                result.canonical_stale += 1
            else:
                result.canonical_absent += 1
            for selected in (*plan.included, *plan.excluded_stale):
                result.observation_ages_seconds.append(selected.observation_age_seconds)
            for selected in plan.excluded_stale:
                book = selected.observation.sportsbook
                result.stale_books[book] = result.stale_books.get(book, 0) + 1
        report.candidates.append(result)

    return report


def render(report: CalibrationReport) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 78)
    add("THRESHOLD CALIBRATION PREVIEW — READ ONLY, NOTHING WRITTEN")
    add("=" * 78)
    add(f"game:            {report.game_id}")
    add(f"taken_at:        {report.taken_at}")
    add(f"canonical book:  {report.canonical_sportsbook}")
    add(f"provider:        {report.market_data_provider}")
    add("")
    add(f"{'tolerance':>12}  {'mkts':>5} {'valid':>6} {'canon-stale':>12} {'excl':>5} {'max age':>10} {'med age':>9}")
    add("-" * 78)
    for c in report.candidates:
        tol = "none" if c.max_observation_age_seconds is None else str(c.max_observation_age_seconds)
        max_age = "-" if c.max_age is None else f"{c.max_age:.1f}"
        med_age = "-" if c.median_age is None else f"{c.median_age:.1f}"
        add(
            f"{tol:>12}  {c.markets:>5} {c.valid_baselines:>6} {c.canonical_stale:>12} "
            f"{c.stale_books_excluded:>5} {max_age:>10} {med_age:>9}"
        )
    add("")
    tightest = report.tightest_without_baseline_loss()
    if tightest is None:
        add("No gated candidate preserved a canonical baseline on every market.")
    else:
        add(
            f"Tightest candidate with no baseline loss: "
            f"{tightest.max_observation_age_seconds}s"
        )
    add("")
    add("This is where the cliff sits for THIS capture. It is not the")
    add("recommendation: the tolerance is a post-refresh-failure grace period,")
    add("and a capture whose refresh succeeded says nothing about how long we")
    add("are willing to run on last-known observations after one fails.")
    add("=" * 78)
    return "\n".join(out)
