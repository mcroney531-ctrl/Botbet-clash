"""Line-shape helpers (RULES.md §6a, §22).

Deliberately not a stored field anywhere (DATABASE.md v2 note, §7):
pushability is a property of the specific quoted/observed line at the
moment it's used, not something that can live permanently on a
`PropMarket` — the same conceptual market can be 74.5 at Opening and 75 at
Final. Compute it at the point of use instead.
"""

from __future__ import annotations

from decimal import Decimal


def is_pushable_line(line: Decimal) -> bool:
    """A line is pushable if it's a whole number (a third "exactly equal"
    outcome is possible). A half-point (or any non-integer) line is
    binary and non-pushable."""

    return line == line.to_integral_value()
