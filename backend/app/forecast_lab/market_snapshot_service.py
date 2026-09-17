"""MarketSnapshotService — builds a frozen `MarketSnapshot` from whatever
`PropQuote` rows already exist as of a point in time. No live provider
calls happen here or after: this only ever reads quotes already persisted
by an ingestion pass that ran, committed, and finished BEFORE the capture
began (seam doc §3.3). RULES.md §16's "if the canonical market is
unavailable... do not silently substitute" is enforced by
`quote_selection.plan_selection`, which decides what this consumes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.markets import MarketSnapshot, PropQuote
from app.db.repositories.market_repository import MarketRepository
from app.marketdata.provenance import SYNTHETIC_SOURCE
from app.forecast_lab.market_math import (
    BookQuote,
    canonical_baseline_is_valid,
    devig_two_sided,
    market_line_context,
    same_line_consensus_probability,
)
from app.forecast_lab.quote_selection import (
    QuoteObservation,
    SelectionPlan,
    plan_selection,
    validate_max_observation_age,
)


def as_observation(row: PropQuote) -> QuoteObservation:
    """ORM row -> the value type the selection rule takes.

    `provider_market_updated_at` is deliberately not carried across. The
    rule is forbidden to consider it, and the cheapest way to keep a
    forbidden field out of a decision is for the decision never to be
    handed it.
    """

    return QuoteObservation(
        quote_id=row.id,
        sportsbook=row.sportsbook,
        line=row.line,
        over_price=row.over_price,
        under_price=row.under_price,
        as_of_at=row.as_of_at,
        retrieved_at=row.retrieved_at,
    )


class MarketSnapshotService:
    def __init__(
        self,
        session: Session,
        *,
        devig_method: str = "PROPORTIONAL_V1",
        market_data_provider: str = SYNTHETIC_SOURCE,
        max_observation_age_seconds: int | None = None,
    ) -> None:
        self.session = session
        self.devig_method = devig_method
        # Both of these are season configuration, resolved by the caller
        # from the active SeasonRules row and passed in. Forecast Lab must
        # not know that vendors exist, let alone which one — it receives a
        # frozen string and pins quote selection to it.
        self.market_data_provider = market_data_provider
        # How old an OBSERVATION may be, at `taken_at`, and still count
        # toward this snapshot. `None` disables the gate, which is the
        # Phase-2/synthetic behaviour and stays the default.
        #
        # Still a caller-supplied parameter rather than a SeasonRules
        # column: this is a post-refresh-failure grace period, and no
        # defensible number exists until the refresh->capture workflow has
        # been exercised against real failures.
        #
        # Validated at CONSTRUCTION, not at first use: a misconfigured
        # tolerance must fail before a run starts, not partway through a
        # slate with some snapshots already written.
        self.max_observation_age_seconds = validate_max_observation_age(max_observation_age_seconds)

    def selection_plan(
        self, *, market_id: uuid.UUID, canonical_sportsbook: str, taken_at: datetime
    ) -> SelectionPlan:
        """What a snapshot at `taken_at` WOULD consume. Reads only.

        Shared with the calibration preview, which has to ask this question
        without writing anything. `build_snapshot` is this plus the
        arithmetic and the INSERT, so a preview and a real capture cannot
        disagree about the selection — which is the only thing that makes
        previewing a threshold evidence about that threshold.
        """

        repo = MarketRepository(self.session)
        rows = repo.quotes_as_of(market_id, source=self.market_data_provider, as_of=taken_at)
        return plan_selection(
            [as_observation(r) for r in rows],
            taken_at=taken_at,
            canonical_sportsbook=canonical_sportsbook,
            max_observation_age_seconds=self.max_observation_age_seconds,
        )

    def build_snapshot(
        self, *, market_id: uuid.UUID, canonical_sportsbook: str, taken_at: datetime
    ) -> MarketSnapshot:
        plan = self.selection_plan(
            market_id=market_id, canonical_sportsbook=canonical_sportsbook, taken_at=taken_at
        )

        quotes = [_book_quote(s) for s in plan.included]
        canonical_selected = plan.canonical
        canonical_quote = _book_quote(canonical_selected) if canonical_selected is not None else None
        # A stale canonical book promotes nobody: RULES.md §16 forbids
        # silently substituting a different sportsbook for the canonical
        # baseline, and "too old to trust" is a form of unavailable. The
        # snapshot and its market context are still written; only the
        # baseline goes invalid, with `canonical_quote_stale` recording
        # which failure it was.
        is_valid = canonical_baseline_is_valid(canonical_quote)

        canonical_line = canonical_over_price = canonical_under_price = None
        canonical_over_prob = canonical_under_prob = None
        consensus = None
        if is_valid:
            assert canonical_quote is not None
            canonical_line = canonical_quote.line
            canonical_over_price = canonical_quote.over_price
            canonical_under_price = canonical_quote.under_price
            canonical_over_prob, canonical_under_prob = devig_two_sided(
                canonical_quote.over_price, canonical_quote.under_price
            )
            consensus = same_line_consensus_probability(canonical_line, quotes)

        context = market_line_context(quotes)

        row = MarketSnapshot(
            market_id=market_id,
            taken_at=taken_at,
            canonical_sportsbook=canonical_sportsbook,
            canonical_line=canonical_line,
            canonical_over_price=canonical_over_price,
            canonical_under_price=canonical_under_price,
            canonical_over_probability=canonical_over_prob,
            canonical_under_probability=canonical_under_prob,
            devig_method=self.devig_method,
            same_line_consensus_over_probability=consensus,
            market_median_line=context["market_median_line"],
            market_min_line=context["market_min_line"],
            market_max_line=context["market_max_line"],
            number_of_books=context["number_of_books"],
            is_valid_canonical_baseline=is_valid,
            max_observation_age_seconds=plan.max_observation_age_seconds,
            books_observed=plan.books_observed,
            stale_books_excluded=plan.stale_books_excluded,
            canonical_quote_stale=plan.canonical_quote_stale,
            selected_quotes=plan.as_record(),
        )
        MarketRepository(self.session).add_market_snapshot(row)
        return row


def _book_quote(selected) -> BookQuote:
    o = selected.observation
    return BookQuote(o.sportsbook, o.line, o.over_price, o.under_price)
