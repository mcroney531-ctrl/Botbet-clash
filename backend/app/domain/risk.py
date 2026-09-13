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
from app.domain.enums import Side, Urgency
from app.domain.models import SeasonRules


def american_to_decimal_odds(price: int) -> Decimal:
    price_d = Decimal(price)
    if -100 < price_d < 100:
        raise ValueError(f"{price} is not a valid American price (must be <= -100 or >= 100)")
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
    ceiling. The result is always floored to `stake_increment` *after*
    taking the min — flooring only the cap and then returning
    `min(requested, floored_cap)` would still let an off-increment
    `model_requested_stake` (e.g. $2.10 under a $3.00 cap) pass straight
    through unfloored, which `record_execution` would then legitimately
    reject. `final_allowed_stake` is supposed to mean "an amount that can
    actually be placed" — that's only true if it's floored last.
    """

    if model_requested_stake.is_negative():
        raise ValueError("model_requested_stake cannot be negative")
    cap = rules.cap_for(available_bankroll, urgency)
    raw_allowed = model_requested_stake.min(cap)
    return raw_allowed.floor_to_increment(rules.stake_increment)


def price_is_acceptable(worst_acceptable_price: int, actual_price: int) -> bool:
    """True if `actual_price` is at least as good for the bettor as
    `worst_acceptable_price` — i.e. the execution didn't get worse than the
    boundary the ticket named.

    For valid American odds (which are never in the open interval
    (-100, 100)), plain signed-integer comparison happens to already be
    monotonic in "how good is this price for the bettor" — -115 > -120
    (better), and any positive price outnumbers any negative one (also
    better) — so `actual_price >= worst_acceptable_price` would score the
    same verdict here. The reason to go through decimal odds anyway: it's
    the version that can't be quietly broken by a sign-unaware refactor
    (e.g. someone "simplifying" this to compare `abs(price)`, which
    inverts the ordering for positive prices), and it fails loudly
    (`ValueError`) instead of silently misordering if a price of 0 or
    something else outside the valid American-odds range ever shows up.
    """

    return american_to_decimal_odds(actual_price) >= american_to_decimal_odds(worst_acceptable_price)


def line_is_acceptable(side: Side, worst_acceptable_line: Decimal, actual_line: Decimal) -> bool:
    """True if `actual_line` has not moved past the ticket's boundary
    against the bettor. Which direction is "against the bettor" depends
    on the side:

    - OVER: a higher line is harder to clear, so the boundary is a
      ceiling — actual_line must be <= worst_acceptable_line.
    - UNDER: a lower line is harder to stay under, so the boundary is a
      floor — actual_line must be >= worst_acceptable_line.
    """

    if side is Side.OVER:
        return actual_line <= worst_acceptable_line
    return actual_line >= worst_acceptable_line
