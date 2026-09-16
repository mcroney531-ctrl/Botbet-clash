"""Vendor market key -> internal StatFamily (seam doc §10).

One-way and explicit. No prefix matching, no fuzzy matching, no
"close enough" fallback. An unrecognized key is quarantined as
UNSUPPORTED_MARKET and produces no quote.

The reason is specific: silently coercing something like
`player_pass_attempts` into `passing_yards` would poison an entire
season of research while never failing a test. A quarantine shows up in
telemetry; a bad coercion does not.

TENTATIVE vs VERIFIED
---------------------
The five spellings below are NOT production-trusted. Two of them were
confirmed against vendor documentation; the full set was not, because the
docs are unreachable from the development sandbox. They live in
TENTATIVE_MARKET_KEYS until the live validation probe prints the keys the
API actually returns.

The split is mechanical, not advisory: `verified_market_keys()` returns
the empty mapping, so any code path that requires verified keys refuses
to run rather than persisting research data built on a guess. Promoting a
key means moving it into VERIFIED_MARKET_KEYS after the probe proves it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from app.domain.enums import StatFamily

TENTATIVE_MARKET_KEYS: Mapping[str, StatFamily] = MappingProxyType(
    {
        "player_pass_yds": StatFamily.PASSING_YARDS,
        "player_pass_tds": StatFamily.PASSING_TOUCHDOWNS,
        "player_rush_yds": StatFamily.RUSHING_YARDS,
        "player_receptions": StatFamily.RECEPTIONS,
        "player_reception_yds": StatFamily.RECEIVING_YARDS,
    }
)
"""Candidate spellings, used ONLY to build probe requests. Never a basis
for production persistence."""

VERIFIED_MARKET_KEYS: Mapping[str, StatFamily] = MappingProxyType({})
"""Spellings proven against a real payload. Empty until the probe runs;
populating it is a deliberate act, reviewed alongside the probe report."""


class UnverifiedMarketMappingError(RuntimeError):
    """Raised when production ingestion is attempted on unverified keys."""


def tentative_market_keys() -> Mapping[str, StatFamily]:
    return TENTATIVE_MARKET_KEYS


def verified_market_keys() -> Mapping[str, StatFamily]:
    return VERIFIED_MARKET_KEYS


def vendor_keys_for(families, *, verified_only: bool) -> tuple[str, ...]:
    """The vendor keys to request for these internal families.

    `verified_only=True` is what production ingestion passes. It raises
    rather than silently returning fewer keys, because a request that
    quietly drops three of five families would look like "DraftKings
    doesn't offer those" in the resulting data.
    """

    table = VERIFIED_MARKET_KEYS if verified_only else TENTATIVE_MARKET_KEYS
    wanted = {StatFamily(f) for f in families}
    keys = tuple(k for k, fam in table.items() if fam in wanted)
    missing = wanted - {fam for k, fam in table.items() if fam in wanted}
    if missing:
        if verified_only:
            raise UnverifiedMarketMappingError(
                "No VERIFIED vendor market key for: "
                + ", ".join(sorted(f.value for f in missing))
                + ". The validation probe must confirm the real spellings from a "
                "live payload before production ingestion may run. Refusing to "
                "fall back to tentative keys."
            )
        raise KeyError(f"no tentative vendor key for {sorted(f.value for f in missing)}")
    return keys


def resolve_stat_family(vendor_key: str, *, verified_only: bool) -> StatFamily | None:
    """Map a returned vendor key to an internal family, or None.

    None means quarantine. It never means "pick something similar."
    """

    table = VERIFIED_MARKET_KEYS if verified_only else TENTATIVE_MARKET_KEYS
    return table.get(vendor_key)
