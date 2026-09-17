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

SELECTION_SCHEMA = "market_snapshot_selection_v1"


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
        # toward this snapshot. `None` disables the gate entirely, which
        # is the Phase-2/synthetic behaviour and stays the default.
        #
        # This is deliberately a caller-supplied parameter rather than a
        # SeasonRules column: no defensible number exists yet. It lands in
        # SeasonRules once real checkpoint captures say what live staleness
        # actually looks like, and inventing a plausible-looking default
        # here would freeze a guess into the research record.
        self.max_observation_age_seconds = max_observation_age_seconds

    def build_snapshot(self, *, market_id: uuid.UUID, canonical_sportsbook: str, taken_at: datetime) -> MarketSnapshot:
        repo = MarketRepository(self.session)
        quote_rows = repo.quotes_as_of(market_id, source=self.market_data_provider, as_of=taken_at)

        # One quote per book: the most recent observation as of `taken_at`.
        # `quotes_as_of` orders as_of_at desc, then retrieved_at desc, then
        # id desc -- deterministic all the way down, which this loop relies
        # on: it takes the first row it sees per sportsbook, so an unstable
        # sort would make the canonical baseline non-reproducible. It
        # orders on OBSERVATION time, not retrieval time; see the docstring
        # on quotes_as_of for why that distinction is load-bearing.
        latest_by_book: dict[str, PropQuote] = {}
        for q in quote_rows:
            if q.sportsbook not in latest_by_book:
                latest_by_book[q.sportsbook] = q

        # Freshness is decided PER BOOK, on OBSERVATION age -- how long
        # before `taken_at` we actually saw that book's state. It is never
        # decided on `provider_market_updated_at`: that is the vendor's
        # claim about when the market last MOVED, so a book that has sat
        # at the same number all week reads as "hours stale" while being
        # perfectly current, and a feed that silently stopped reporting
        # reads as fresh right up until it moves. Seam doc §3.1.
        included: dict[str, PropQuote] = {}
        excluded: dict[str, PropQuote] = {}
        for sportsbook, q in latest_by_book.items():
            if self._is_stale(q, taken_at):
                excluded[sportsbook] = q
            else:
                included[sportsbook] = q

        # The canonical book being stale is NOT a reason to promote the
        # next-freshest book: RULES.md §16 forbids silently substituting a
        # different sportsbook for the canonical baseline. The snapshot is
        # written, the market context is written, and the baseline is
        # simply marked invalid -- with `canonical_quote_stale` to
        # distinguish "the book was quoting, we just had nothing recent
        # enough" from "the book was not in the feed at all".
        canonical_quote_stale = canonical_sportsbook in excluded

        quotes = [
            BookQuote(q.sportsbook, q.line, q.over_price, q.under_price)
            for q in sorted(included.values(), key=lambda row: row.sportsbook)
        ]
        canonical_row = included.get(canonical_sportsbook)
        canonical_quote = (
            BookQuote(canonical_row.sportsbook, canonical_row.line, canonical_row.over_price, canonical_row.under_price)
            if canonical_row is not None
            else None
        )
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
            max_observation_age_seconds=self.max_observation_age_seconds,
            stale_books_excluded=len(excluded),
            canonical_quote_stale=canonical_quote_stale,
            selected_quotes=self._selection_record(
                taken_at=taken_at,
                canonical_sportsbook=canonical_sportsbook,
                included=included,
                excluded=excluded,
            ),
        )
        repo.add_market_snapshot(row)
        return row

    def _is_stale(self, quote: PropQuote, taken_at: datetime) -> bool:
        if self.max_observation_age_seconds is None:
            return False
        return observation_age_seconds(quote, taken_at) > self.max_observation_age_seconds

    def _selection_record(
        self,
        *,
        taken_at: datetime,
        canonical_sportsbook: str,
        included: dict[str, PropQuote],
        excluded: dict[str, PropQuote],
    ) -> dict:
        def entry(q: PropQuote) -> dict:
            return {
                "quote_id": str(q.id),
                "sportsbook": q.sportsbook,
                "as_of_at": q.as_of_at.isoformat(),
                "observation_age_seconds": observation_age_seconds(q, taken_at),
                "is_canonical": q.sportsbook == canonical_sportsbook,
            }

        def entries(rows: dict[str, PropQuote]) -> list[dict]:
            return [entry(rows[book]) for book in sorted(rows)]

        return {
            "schema": SELECTION_SCHEMA,
            "included": entries(included),
            "excluded_stale": entries(excluded),
        }


def observation_age_seconds(quote: PropQuote, taken_at: datetime) -> float:
    """How long before `taken_at` this observation was made.

    Clamped at zero rather than allowed to go negative. `quotes_as_of`
    already filters `as_of_at <= taken_at`, so a negative value would mean
    a bug upstream rather than a fresh quote — but an age metric that can
    read "-3.0 seconds" invites exactly the sign confusion that made the
    first pass at this measure tautological, so the clamp is explicit.
    """

    return max(0.0, (taken_at - quote.as_of_at).total_seconds())
