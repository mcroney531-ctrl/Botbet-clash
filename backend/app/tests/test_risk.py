from decimal import Decimal

import pytest

from app.core.money import Money
from app.domain.enums import Urgency
from app.domain.models import SeasonRules
from app.domain.risk import (
    american_to_decimal_odds,
    full_kelly_fraction,
    kelly_reference_stake,
    resolve_final_allowed_stake,
)


def rules() -> SeasonRules:
    return SeasonRules(
        rules_version="test",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


def test_american_to_decimal_odds():
    assert american_to_decimal_odds(-120) == pytest.approx(Decimal("1.8333"), rel=Decimal("0.001"))
    assert american_to_decimal_odds(150) == Decimal("2.5")


def test_full_kelly_no_edge_is_zero():
    # -110 implies ~52.4% breakeven; a 50% true probability has no edge.
    assert full_kelly_fraction(Decimal("0.50"), -110) == Decimal(0)


def test_full_kelly_positive_edge():
    # True probability well above breakeven should recommend a positive fraction.
    f = full_kelly_fraction(Decimal("0.65"), -110)
    assert f > Decimal(0)


def test_kelly_reference_stake_scales_with_fraction():
    bankroll = Money.from_dollars_str("50.00")
    full = full_kelly_fraction(Decimal("0.65"), -110)
    fractional_stake = kelly_reference_stake(rules(), bankroll, Decimal("0.65"), -110)
    assert fractional_stake == bankroll.fraction(full * Decimal("0.20"))


def test_resolve_final_allowed_stake_caps_standard():
    bankroll = Money.from_dollars_str("50.00")
    r = rules()
    requested = Money.from_dollars_str("14.00")
    final = resolve_final_allowed_stake(r, bankroll, Urgency.STRONG, requested)
    assert final == Money.from_dollars_str("10.00")  # 20% of $50


def test_resolve_final_allowed_stake_pounce_uses_exceptional_cap():
    bankroll = Money.from_dollars_str("50.00")
    r = rules()
    requested = Money.from_dollars_str("100.00")
    final = resolve_final_allowed_stake(r, bankroll, Urgency.POUNCE, requested)
    assert final == Money.from_dollars_str("15.00")  # 30% of $50


def test_model_may_request_less_than_cap():
    bankroll = Money.from_dollars_str("50.00")
    r = rules()
    requested = Money.from_dollars_str("2.00")
    final = resolve_final_allowed_stake(r, bankroll, Urgency.STRONG, requested)
    assert final == requested
