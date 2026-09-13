"""Canonical market math (RULES.md §5, §17). Pure functions only — no
session, no I/O. `PROPORTIONAL_V1` is the frozen V1 de-vig method.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

# Matches DATABASE.md's NUMERIC(6,5) for stored probabilities. A repeating
# fraction (e.g. -110 -> 110/210) can never terminate exactly in any fixed
# decimal context, so composing several such values through addition and
# division (proportional de-vig) accumulates sub-ULP noise however high the
# working precision is set. Quantizing at the point a probability becomes
# storable — rather than chasing more digits of an intermediate that was
# never going to be exact anyway — is what actually makes e.g. a -110/-110
# pick'em round-trip to precisely 0.5.
PROBABILITY_PLACES = Decimal("0.00001")


def raw_implied_probability(price: int) -> Decimal:
    """American price -> raw (vig-inflated) implied probability, exactly
    RULES.md §5's formula.

    This is algebraically equivalent to `1 / american_to_decimal_odds(price)`
    (`domain/risk.py`), but is computed directly rather than composed
    through that extra division — composing the two independently-rounded
    `Decimal` divisions measurably drifts from the exact RULES.md formula
    (e.g. -110 no longer round-trips to precisely 0.5 after de-vigging a
    pick'em). Forecast Lab probabilities are graded to five decimal places
    (DATABASE.md's `NUMERIC(6,5)`), so this isn't cosmetic.
    """

    price_d = Decimal(price)
    if -100 < price_d < 100:
        raise ValueError(f"{price} is not a valid American price (must be <= -100 or >= 100)")
    if price_d < 0:
        return (-price_d) / (-price_d + 100)
    return Decimal(100) / (price_d + 100)


def proportional_devig(p_over_raw: Decimal, p_under_raw: Decimal) -> tuple[Decimal, Decimal]:
    """PROPORTIONAL_V1 (RULES.md §5): normalize the two raw probabilities
    so they sum to exactly 1, removing the sportsbook's vig."""

    total = p_over_raw + p_under_raw
    if total <= 0:
        raise ValueError("raw probabilities must sum to a positive number")
    return (p_over_raw / total, p_under_raw / total)


def devig_two_sided(over_price: int, under_price: int) -> tuple[Decimal, Decimal]:
    """American over/under prices -> de-vigged (p_over, p_under), quantized
    to the storable precision (see `PROBABILITY_PLACES`).

    `p_under` is derived as `1 - p_over` *after* quantizing `p_over`,
    rather than quantizing both independently — this is a strictly
    binary market, so the two must sum to exactly 1; quantizing each side
    on its own could drift them apart by up to 2 units in the last place.
    """

    p_over_raw, p_under_raw = proportional_devig(raw_implied_probability(over_price), raw_implied_probability(under_price))
    p_over = p_over_raw.quantize(PROBABILITY_PLACES)
    return (p_over, Decimal(1) - p_over)


@dataclass(frozen=True, slots=True)
class BookQuote:
    """The minimal shape market math needs from a `PropQuote` row — kept
    independent of the ORM so these functions stay pure/unit-testable."""

    sportsbook: str
    line: Decimal
    over_price: int
    under_price: int


def same_line_consensus_probability(canonical_line: Decimal, quotes: list[BookQuote]) -> Decimal | None:
    """RULES.md §17: only books quoting the *same line* as canonical may
    contribute to the consensus probability — a median of their de-vigged
    over-probabilities. A different line is market context, never
    averaged in as if it were a probability for the same event."""

    same_line = [q for q in quotes if q.line == canonical_line]
    if not same_line:
        return None
    probs = [devig_two_sided(q.over_price, q.under_price)[0] for q in same_line]
    return statistics.median(probs).quantize(PROBABILITY_PLACES)


def market_line_context(quotes: list[BookQuote]) -> dict:
    """RULES.md §17's diagnostic-only market-line context — never treated
    as a probability."""

    lines = [q.line for q in quotes]
    return {
        "market_median_line": statistics.median(lines) if lines else None,
        "market_min_line": min(lines) if lines else None,
        "market_max_line": max(lines) if lines else None,
        "number_of_books": len(quotes),
    }


def canonical_baseline_is_valid(canonical_quote: BookQuote | None) -> bool:
    """RULES.md §19's checklist, reduced to what's checkable from the
    quote itself — a valid market/proposition/line and a timestamped
    snapshot are guaranteed by construction (the quote is already tied to
    one `PropMarket` and one `retrieved_at`); what remains to check here
    is that both sides are actually priced."""

    if canonical_quote is None:
        return False
    return canonical_quote.over_price is not None and canonical_quote.under_price is not None
