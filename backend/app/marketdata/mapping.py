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
All five spellings below are documented by the vendor (confirmed against
The Odds API's current primary docs, 2026-09-16). They are no longer
guesses -- but they are still not OBSERVED: no live payload has returned
them to us. Documentation and observation are different evidence, and this
gate tracks the second, so they stay in TENTATIVE_MARKET_KEYS until the
probe prints what actually comes back.

Alternate NFL player props are documented under separate `_alternate`
market keys. That is why this table needs no alternate-handling logic:
unlisted keys are simply never mapped. The ingestion-level ambiguity
quarantine remains as defence in depth.

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
"""Vendor-documented spellings, used to build probe requests. Documented
is not observed, so these are never a basis for production persistence."""

VERIFIED_MARKET_KEYS: Mapping[str, StatFamily] = MappingProxyType(
    {
        "player_pass_yds": StatFamily.PASSING_YARDS,
        "player_pass_tds": StatFamily.PASSING_TOUCHDOWNS,
        "player_rush_yds": StatFamily.RUSHING_YARDS,
        "player_receptions": StatFamily.RECEPTIONS,
        "player_reception_yds": StatFamily.RECEIVING_YARDS,
    }
)
"""Spellings OBSERVED in a real payload.

Promoted 2026-09-17 by the live validation probe (ingestion_run
16f44ea0-3664-404e-85ab-2d33140e6625), which requested all five against a
real NFL event and received all five back verbatim, with DraftKings
quoting every one of the five internal families. Documentation alone was
never sufficient to populate this table -- observation was."""


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
