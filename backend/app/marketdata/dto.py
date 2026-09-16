"""Normalized provider DTOs (seam doc §9).

The shapes that cross the adapter boundary. Decimal for lines and int for
American prices, matching PropQuote's Numeric(6,2)/Integer exactly, so no
lossy float ever enters the pipeline.

Deliberately absent: raw vendor JSON, vendor response objects, request
URLs, API keys. Raw preservation happens once per call in
provider_calls, not smuggled through every quote.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.enums import StatFamily


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware; got a naive datetime")
    return value.astimezone(timezone.utc)


def normalize_player_name(raw: str) -> str:
    """Conservative normalization for the no-stable-id fallback.

    NFKC, case-fold, collapse whitespace. That is the whole list.

    Suffixes (Jr., Sr., II, III) are NEVER stripped: the NFL has live
    father/son and same-family pairs, and stripping a suffix would merge
    two distinct people into one identity permanently. No punctuation
    heuristics and no fuzzy matching either -- an over-eager normalizer
    corrupts the research record silently, whereas an over-conservative
    one merely creates a duplicate someone can reconcile later.
    """

    folded = unicodedata.normalize("NFKC", raw).casefold()
    return " ".join(folded.split())


@dataclass(frozen=True)
class ProviderEventRef:
    provider: str
    external_event_id: str

    def as_external_ref(self) -> str:
        """The value stored in Game.external_ref.

        Provider-scoped rather than the bare vendor id so that adding a
        second provider later cannot collide with this one's id space.
        """

        return f"{self.provider}:{self.external_event_id}"


@dataclass(frozen=True)
class ProviderEvent:
    ref: ProviderEventRef
    sport_key: str
    kickoff_at: datetime
    home_team: str
    away_team: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_at", _require_utc(self.kickoff_at, "kickoff_at"))


@dataclass(frozen=True)
class ProviderPlayerRef:
    """Player identity as the provider expresses it.

    `external_player_id` is None when the provider exposes no stable
    identifier. Whether that is the case for this vendor is an OPEN
    question the validation probe must answer; until it does, production
    player persistence stays blocked rather than guessing.
    """

    provider: str
    display_name: str
    external_player_id: str | None = None
    team: str | None = None
    position: str | None = None

    @property
    def has_stable_id(self) -> bool:
        return bool(self.external_player_id)

    def as_external_ref(self) -> str:
        """The value stored in Player.external_ref.

        The scheme is embedded in the ref itself (`:id:` vs `:name:`) so a
        later migration can tell at a glance which rows were identified by
        a real provider id and which fell back to a normalized name.
        """

        if self.external_player_id:
            return f"{self.provider}:id:{self.external_player_id}"
        return f"{self.provider}:name:{normalize_player_name(self.display_name)}"


@dataclass(frozen=True)
class ProviderQuote:
    """One fully normalized two-sided quote.

    Emitted only when both Over and Under are present at the same line —
    see the pairing rule in the adapter. A one-sided price is a
    diagnostic, never a quote with a null side.
    """

    event: ProviderEventRef
    player: ProviderPlayerRef
    stat_family: StatFamily
    vendor_market_key: str
    sportsbook: str
    line: Decimal
    over_price: int
    under_price: int
    as_of_at: datetime
    retrieved_at: datetime
    source: str
    provider_market_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of_at", _require_utc(self.as_of_at, "as_of_at"))
        object.__setattr__(self, "retrieved_at", _require_utc(self.retrieved_at, "retrieved_at"))
        if self.provider_market_updated_at is not None:
            object.__setattr__(
                self,
                "provider_market_updated_at",
                _require_utc(self.provider_market_updated_at, "provider_market_updated_at"),
            )
        # A provider claiming a snapshot from the future is a malformed
        # response, not a quote (seam §3).
        if self.as_of_at > self.retrieved_at:
            raise ValueError(
                f"as_of_at ({self.as_of_at.isoformat()}) is after retrieved_at "
                f"({self.retrieved_at.isoformat()}): a market state cannot be "
                "observed before it existed"
            )
        if not isinstance(self.line, Decimal):
            raise TypeError("line must be a Decimal; float lines lose precision")
        if isinstance(self.over_price, bool) or isinstance(self.under_price, bool):
            raise TypeError("prices must be int, not bool")
        if not isinstance(self.over_price, int) or not isinstance(self.under_price, int):
            raise TypeError("American prices must be int")
