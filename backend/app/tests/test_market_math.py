import statistics
from decimal import Decimal

from app.forecast_lab.market_math import (
    PROBABILITY_PLACES,
    BookQuote,
    canonical_baseline_is_valid,
    devig_two_sided,
    market_line_context,
    proportional_devig,
    raw_implied_probability,
    same_line_consensus_probability,
)


def test_raw_implied_probability_matches_rules_formula():
    # RULES.md §5: negative odds p = |odds|/(|odds|+100)
    assert raw_implied_probability(-120) == Decimal(120) / Decimal(220)
    # positive odds p = 100/(odds+100)
    assert raw_implied_probability(150) == Decimal(100) / Decimal(250)


def test_proportional_devig_sums_to_one():
    p_over, p_under = proportional_devig(Decimal("0.55"), Decimal("0.50"))
    assert p_over + p_under == Decimal(1)
    assert p_over > p_under  # preserves relative weighting


def test_devig_two_sided_removes_vig():
    # -110/-110 is a standard ~4.5% vig line; de-vigged should land at 50/50.
    p_over, p_under = devig_two_sided(-110, -110)
    assert p_over == Decimal("0.5")
    assert p_under == Decimal("0.5")


def test_same_line_consensus_ignores_other_lines():
    quotes = [
        BookQuote("DRAFTKINGS", Decimal("52.5"), -115, -105),
        BookQuote("FANDUEL", Decimal("52.5"), -110, -110),
        BookQuote("CAESARS", Decimal("54.5"), -120, +100),  # different line - excluded
    ]
    consensus = same_line_consensus_probability(Decimal("52.5"), quotes)
    p1 = devig_two_sided(-115, -105)[0]
    p2 = devig_two_sided(-110, -110)[0]
    assert consensus == statistics.median([p1, p2]).quantize(PROBABILITY_PLACES)


def test_same_line_consensus_none_when_no_book_matches_canonical_line():
    quotes = [BookQuote("CAESARS", Decimal("54.5"), -120, 100)]
    assert same_line_consensus_probability(Decimal("52.5"), quotes) is None


def test_market_line_context_reports_diagnostics_not_a_probability():
    quotes = [
        BookQuote("DRAFTKINGS", Decimal("52.5"), -115, -105),
        BookQuote("FANDUEL", Decimal("52.5"), -110, -110),
        BookQuote("CAESARS", Decimal("54.5"), -120, 100),
    ]
    ctx = market_line_context(quotes)
    assert ctx["market_min_line"] == Decimal("52.5")
    assert ctx["market_max_line"] == Decimal("54.5")
    assert ctx["number_of_books"] == 3


def test_canonical_baseline_validity():
    assert canonical_baseline_is_valid(None) is False
    assert canonical_baseline_is_valid(BookQuote("DRAFTKINGS", Decimal("52.5"), -115, -105)) is True
