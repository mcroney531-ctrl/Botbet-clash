"""Integer-cents money type.

Constitution / RULES.md are explicit: money is never floating point. Every
bankroll figure in the domain layer flows through this type so a stray
`float` can't sneak in from application code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True, slots=True)
class Money:
    cents: int

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int) or isinstance(self.cents, bool):
            raise TypeError(f"Money requires an int cent amount, got {self.cents!r}")

    @classmethod
    def zero(cls) -> "Money":
        return cls(0)

    @classmethod
    def from_cents(cls, cents: int) -> "Money":
        return cls(int(cents))

    @classmethod
    def from_dollars_str(cls, dollars: str) -> "Money":
        """Parse a decimal dollar string (e.g. "15.00") without float error."""
        as_cents = (Decimal(dollars) * 100).to_integral_value(rounding=ROUND_DOWN)
        return cls(int(as_cents))

    def __add__(self, other: "Money") -> "Money":
        return Money(self.cents + other.cents)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.cents - other.cents)

    def __neg__(self) -> "Money":
        return Money(-self.cents)

    def __lt__(self, other: "Money") -> bool:
        return self.cents < other.cents

    def __le__(self, other: "Money") -> bool:
        return self.cents <= other.cents

    def __gt__(self, other: "Money") -> bool:
        return self.cents > other.cents

    def __ge__(self, other: "Money") -> bool:
        return self.cents >= other.cents

    def fraction(self, fraction: Decimal) -> "Money":
        """Return `fraction` of this amount, floored to the cent.

        Flooring (never rounding up) means a stake-cap calculation can
        never accidentally grant a competitor more than the rule allows.
        """
        if fraction < 0:
            raise ValueError("fraction must be non-negative")
        raw = (Decimal(self.cents) * fraction).to_integral_value(rounding=ROUND_DOWN)
        return Money(int(raw))

    def min(self, other: "Money") -> "Money":
        return self if self.cents <= other.cents else other

    def floor_to_increment(self, increment: "Money") -> "Money":
        """Round down to the nearest multiple of `increment` (e.g. a
        sportsbook's practical stake increment). A cap that isn't itself
        placeable in whole increments is not actually a usable ceiling."""

        if increment.cents <= 0:
            raise ValueError("increment must be positive")
        return Money((self.cents // increment.cents) * increment.cents)

    def is_negative(self) -> bool:
        return self.cents < 0

    def as_dollars_str(self) -> str:
        sign = "-" if self.cents < 0 else ""
        whole, remainder = divmod(abs(self.cents), 100)
        return f"{sign}${whole}.{remainder:02d}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.as_dollars_str()
