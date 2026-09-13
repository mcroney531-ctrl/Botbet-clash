"""Minimal Kelly-reference and stake-cap math (RULES.md §10, constitution
§62-70).

This is intentionally small: the full Risk Engine (Phase 5) is where
confidence/uncertainty-aware stake *proposals* come from a model via
`CompetitorAgent.propose_stake`. What lives here is the fixed,
non-negotiable math underneath that a model's requested stake is always
validated against — the Kelly reference point and the hard bankroll-%
caps — so Phase 1 can exercise a believable ticket lifecycle without
waiting on the AI orchestrator.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.money import Money
from app.domain.enums import Urgency
from app.domain.models import SeasonRules


def american_to_decimal_odds(price: int) -> Decimal:
    price_d = Decimal(price)
    if price_d == 0:
        raise ValueError("American price cannot be zero")
    if price_d < 0:
        return Decimal(100) / (-price_d) + 1
    return price_d / Decimal(100) + 1


def full_kelly_fraction(win_probability: Decimal, american_price: int) -> Decimal:
    """Full-Kelly fraction of bankroll for a single binary wager.

    f* = (b*p - q) / b, where b is net decimal odds, p is the model's win
    probability, q = 1-p. Clamped to zero when the math says "no edge" —
    Kelly never recommends betting against your own estimate.
    """

    if not (Decimal(0) < win_probability < Decimal(1)):
        raise ValueError("win_probability must be strictly between 0 and 1")
    decimal_odds = american_to_decimal_odds(american_price)
    b = decimal_odds - 1
    q = Decimal(1) - win_probability
    edge_fraction = (b * win_probability - q) / b
    return max(edge_fraction, Decimal(0))


def kelly_reference_stake(
    rules: SeasonRules,
    available_bankroll: Money,
    win_probability: Decimal,
    american_price: int,
) -> Money:
    """Fractional-Kelly reference stake — informational, not a cap."""

    fractional = full_kelly_fraction(win_probability, american_price) * rules.kelly_fraction
    return available_bankroll.fraction(fractional)


def resolve_final_allowed_stake(
    rules: SeasonRules,
    available_bankroll: Money,
    urgency: Urgency,
    model_requested_stake: Money,
) -> Money:
    """Apply the hard competition cap (RULES.md §10) to a model's request.

    The model may always request less than the cap; it may never exceed
    it. This never looks at the Kelly reference — that number is stored
    for analysis (RULES.md §10 / constitution §64), not enforced as a
    ceiling.
    """

    if model_requested_stake.is_negative():
        raise ValueError("model_requested_stake cannot be negative")
    cap = rules.cap_for(available_bankroll, urgency)
    return model_requested_stake.min(cap)
