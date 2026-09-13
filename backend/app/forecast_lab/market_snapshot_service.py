"""MarketSnapshotService — builds a frozen `MarketSnapshot` from whatever
`PropQuote` rows already exist as of a point in time. No live provider
calls happen here or after: this only ever reads quotes already persisted
by an ingestion pipeline (not built in Phase 2 — see RULES.md §16's
"if the canonical market is unavailable... do not silently substitute").
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.markets import MarketSnapshot
from app.db.repositories.market_repository import MarketRepository
from app.forecast_lab.market_math import (
    BookQuote,
    canonical_baseline_is_valid,
    devig_two_sided,
    market_line_context,
    same_line_consensus_probability,
)


class MarketSnapshotService:
    def __init__(self, session: Session, *, devig_method: str = "PROPORTIONAL_V1") -> None:
        self.session = session
        self.devig_method = devig_method

    def build_snapshot(self, *, market_id: uuid.UUID, canonical_sportsbook: str, taken_at: datetime) -> MarketSnapshot:
        repo = MarketRepository(self.session)
        quote_rows = repo.quotes_as_of(market_id, taken_at)

        # One quote per book: the most recent as-of `taken_at` (quote_rows
        # is already ordered retrieved_at desc).
        latest_by_book: dict[str, BookQuote] = {}
        for q in quote_rows:
            if q.sportsbook not in latest_by_book:
                latest_by_book[q.sportsbook] = BookQuote(q.sportsbook, q.line, q.over_price, q.under_price)
        quotes = list(latest_by_book.values())

        canonical_quote = latest_by_book.get(canonical_sportsbook)
        is_valid = canonical_baseline_is_valid(canonical_quote)

        canonical_line = canonical_over_price = canonical_under_price = None
        canonical_over_prob = canonical_under_prob = None
        consensus = None
        if is_valid:
            assert canonical_quote is not None
            canonical_line = canonical_quote.line
            canonical_over_price = canonical_quote.over_price
            canonical_under_price = canonical_quote.under_price
            canonical_over_prob, canonical_under_prob = devig_two_sided(canonical_quote.over_price, canonical_quote.under_price)
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
        )
        repo.add_market_snapshot(row)
        return row
