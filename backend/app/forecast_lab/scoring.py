"""Proper scoring rules (RULES.md §34-35). Pure functions, no I/O. Score
GPT/Claude/Gemini/canonical market/same-line consensus all through these
same two functions against the same derived binary outcome — never a
different formula per competitor.
"""

from __future__ import annotations

import math
from decimal import Decimal

_EPSILON = Decimal("0.00001")  # avoids log(0); matches the NUMERIC(6,5) probability precision


def outcome_to_actual(outcome: str) -> Decimal:
    if outcome not in ("OVER", "UNDER"):
        raise ValueError(f"outcome must be OVER or UNDER, got {outcome!r}")
    return Decimal(1) if outcome == "OVER" else Decimal(0)


def brier_score(probability_over: Decimal, outcome: str) -> Decimal:
    """(model_probability_over - outcome)^2 — lower is better."""

    actual = outcome_to_actual(outcome)
    return (probability_over - actual) ** 2


def log_loss(probability_over: Decimal, outcome: str) -> float:
    """Secondary proper scoring rule (RULES.md §35) — more sensitive to
    extreme overconfidence than Brier. Returns a plain float: log loss is
    inherently irrational for any probability other than 0/1, so there is
    no exact Decimal result to preserve the way there is for Brier; this
    is an analysis output, not a stored probability or money field.
    """

    p_over = probability_over
    if outcome == "UNDER":
        p_over = Decimal(1) - p_over
    p_clamped = min(max(p_over, _EPSILON), Decimal(1) - _EPSILON)
    return -math.log(float(p_clamped))
