"""Shared CLI argument parsing for numeric lists.

PowerShell treats an unquoted `30,120` as an ARRAY, and joins it back into
a single space-separated token before handing it to a native command. So a
perfectly reasonable `--backoff-seconds 30,120` arrives as the one string
`"30 120"` and a naive `split(",")` then hands `float()` something it
cannot parse -- which is how a one-shot production command came to die
with a raw traceback over a shell quoting rule.

These parsers accept commas, whitespace, or both, and fail with an
argparse message rather than an exception trace. A command an operator
runs once, against frozen research rules, should explain itself when the
input is wrong.
"""

from __future__ import annotations

import argparse
import math
import re

_SEPARATORS = re.compile(r"[,\s]+")


def split_number_list(raw: str) -> list[str]:
    return [part for part in _SEPARATORS.split(raw.strip()) if part]


def number_list(raw: str, *, field: str = "value") -> list[float]:
    """Comma- and/or whitespace-separated non-negative finite numbers."""

    values: list[float] = []
    for part in split_number_list(raw):
        try:
            value = float(part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{field}: {part!r} is not a number (got {raw!r}). Separate values "
                "with commas or spaces; in PowerShell, quote the whole list."
            ) from None
        if not math.isfinite(value) or value < 0:
            raise argparse.ArgumentTypeError(f"{field}: {value!r} must be finite and >= 0")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError(f"{field}: no values in {raw!r}")
    return values


def int_list(raw: str, *, field: str = "value") -> list[int]:
    """As `number_list`, but every entry must be a whole number.

    A fractional count is rejected rather than truncated: `int(3.7)` is 3,
    and silently accepting that in a frozen rule would change behaviour
    without changing what anyone typed.
    """

    values: list[int] = []
    for part in split_number_list(raw):
        try:
            value = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{field}: {part!r} is not an integer (got {raw!r}). Separate values "
                "with commas or spaces; in PowerShell, quote the whole list."
            ) from None
        if value < 0:
            raise argparse.ArgumentTypeError(f"{field}: {value} must be >= 0")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError(f"{field}: no values in {raw!r}")
    return values
