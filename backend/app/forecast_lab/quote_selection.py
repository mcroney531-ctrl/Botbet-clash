"""The quote-selection rule, as a pure function (seam doc §3.2).

This is the ONE implementation of "which observations does a snapshot at
`taken_at` consume, and which does it refuse as stale". It is pure --- no
session, no ORM, no I/O --- so that the calibration preview can answer
"what WOULD this threshold have selected" without writing a MarketSnapshot,
a CheckpointRun or an EvidenceSnapshot, and without a second copy of the
rule that could drift from the one production uses.

A preview that re-implements the rule proves nothing about the rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

SELECTION_SCHEMA = "market_snapshot_selection_v1"


class FreshnessConfigError(ValueError):
    """The configured tolerance is not a usable threshold."""


def validate_max_observation_age(value: int | None) -> int | None:
    """`None` disables the gate; any other value must be a non-negative
    integer number of seconds.

    A negative tolerance is not a strict rule, it is a configuration
    error: it classifies EVERY observation as stale, so every snapshot
    silently loses its canonical baseline and the run looks like a total
    market outage. Booleans are rejected too --- `True` is an `int` in
    Python and would quietly become a one-second tolerance.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FreshnessConfigError(
            f"max_observation_age_seconds must be None or an int, got {value!r}"
        )
    if value < 0:
        raise FreshnessConfigError(
            f"max_observation_age_seconds must be >= 0, got {value}. A negative "
            "tolerance marks every observation stale, which reads downstream as a "
            "total market outage rather than as the misconfiguration it is."
        )
    return value


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    """The minimal view of a `PropQuote` the selection rule needs.

    Deliberately not the ORM row: keeping this a plain value type is what
    lets the rule be exercised without a database, and stops the rule from
    reaching for a column (`provider_market_updated_at`, say) that it is
    forbidden to consider. The forbidden field is simply not here.
    """

    quote_id: uuid.UUID
    sportsbook: str
    line: Decimal
    over_price: int
    under_price: int
    as_of_at: datetime
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class SelectedQuote:
    observation: QuoteObservation
    observation_age_seconds: float
    is_canonical: bool

    def as_record(self) -> dict:
        return {
            "quote_id": str(self.observation.quote_id),
            "sportsbook": self.observation.sportsbook,
            "as_of_at": self.observation.as_of_at.isoformat(),
            "observation_age_seconds": self.observation_age_seconds,
            "is_canonical": self.is_canonical,
        }


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    """What a snapshot at `taken_at` would consume and what it would refuse."""

    taken_at: datetime
    canonical_sportsbook: str
    max_observation_age_seconds: int | None
    included: tuple[SelectedQuote, ...]
    excluded_stale: tuple[SelectedQuote, ...]

    @property
    def books_observed(self) -> int:
        """Every book that had any quote at `taken_at`.

        `len(included) + len(excluded_stale)` by construction --- a book
        must never fall out of both counts, which is how "the feed dropped
        a book" turns into a snapshot that merely looks thin.
        """

        return len(self.included) + len(self.excluded_stale)

    @property
    def canonical(self) -> SelectedQuote | None:
        for q in self.included:
            if q.is_canonical:
                return q
        return None

    @property
    def canonical_quote_stale(self) -> bool:
        """The canonical book WAS quoting; we just had nothing recent
        enough to trust. Distinct from the canonical book being absent
        from the feed entirely, which is a different failure."""

        return any(q.is_canonical for q in self.excluded_stale)

    @property
    def stale_books_excluded(self) -> int:
        return len(self.excluded_stale)

    def as_record(self) -> dict:
        """The frozen selection record stored on `MarketSnapshot`."""

        return {
            "schema": SELECTION_SCHEMA,
            "included": [q.as_record() for q in self.included],
            "excluded_stale": [q.as_record() for q in self.excluded_stale],
        }


def observation_age_seconds(as_of_at: datetime, taken_at: datetime) -> float:
    """How long before `taken_at` this observation was made.

    Clamped at zero rather than allowed to go negative. Selection already
    filters `as_of_at <= taken_at`, so a negative value would mean a bug
    upstream rather than a fresh quote --- but an age metric that can read
    "-3.0 seconds" invites exactly the sign confusion that made the first
    pass at this measure tautological, so the clamp is explicit.
    """

    return max(0.0, (taken_at - as_of_at).total_seconds())


def plan_selection(
    observations: list[QuoteObservation],
    *,
    taken_at: datetime,
    canonical_sportsbook: str,
    max_observation_age_seconds: int | None,
) -> SelectionPlan:
    """Newest quote per book as of `taken_at`, then the freshness gate.

    Freshness is decided PER BOOK, on OBSERVATION age --- how long before
    `taken_at` we actually saw that book's state. Never on the vendor's
    market-change time: a book that has sat at the same number all week
    would read as hours stale while being perfectly current, and a feed
    that silently stopped reporting would read as fresh right up until it
    moved. Seam doc §3.1.

    Observations later than `taken_at` are dropped outright. A caller that
    hands over the future is asking what a snapshot at `taken_at` saw, and
    the answer cannot include things that had not happened yet.
    """

    max_age = validate_max_observation_age(max_observation_age_seconds)

    eligible = [o for o in observations if o.as_of_at <= taken_at]

    # Sorted here rather than trusted from the caller. The repository's
    # ORDER BY already produces this order, but the calibration preview
    # may assemble observations from anywhere, and "whichever row happened
    # to come first" silently deciding the canonical baseline is precisely
    # the non-reproducibility this ordering exists to prevent.
    eligible.sort(key=lambda o: (o.as_of_at, o.retrieved_at, o.quote_id), reverse=True)

    newest_by_book: dict[str, QuoteObservation] = {}
    for o in eligible:
        newest_by_book.setdefault(o.sportsbook, o)

    included: list[SelectedQuote] = []
    excluded: list[SelectedQuote] = []
    for sportsbook in sorted(newest_by_book):
        o = newest_by_book[sportsbook]
        age = observation_age_seconds(o.as_of_at, taken_at)
        selected = SelectedQuote(
            observation=o,
            observation_age_seconds=age,
            is_canonical=sportsbook == canonical_sportsbook,
        )
        # `>` not `>=`: an age exactly equal to the tolerance is fresh.
        if max_age is not None and age > max_age:
            excluded.append(selected)
        else:
            included.append(selected)

    return SelectionPlan(
        taken_at=taken_at,
        canonical_sportsbook=canonical_sportsbook,
        max_observation_age_seconds=max_age,
        included=tuple(included),
        excluded_stale=tuple(excluded),
    )
