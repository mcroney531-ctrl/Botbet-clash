"""Provider-neutral roster/identity contract.

Third external boundary, sibling to app/ai/ and app/marketdata/. Nothing
above a provider adapter may reference a vendor column name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Literal, Protocol, TypeVar

from app.marketdata.base import ProviderCallMetadata
from app.rosterdata.teams import CanonicalTeam

RosterErrorCategory = Literal[
    "ROSTER_SOURCE_UNAVAILABLE",
    "ROSTER_TIMEOUT",
    "MALFORMED_ROSTER_RESPONSE",
    "UNSUPPORTED_SEASON_WEEK",
    "UNKNOWN_ROSTER_ERROR",
]

ResolutionOutcome = Literal[
    "RESOLVED",
    "UNRESOLVED_PLAYER",
    "AMBIGUOUS_PLAYER",
    "TEAM_GAME_MISMATCH",
    "MISSING_STABLE_ID",
    "ROSTER_IDENTITY_CONFLICT",
    "STALE_ROSTER_SNAPSHOT",
]

RosterBasis = Literal[
    "CURRENT_CONTEMPORANEOUS",
    "ARCHIVED_CONTEMPORANEOUS",
    "HISTORICAL_RECONSTRUCTED",
]

# Live freshness tolerance (seam §13, locked at review). A roster snapshot
# used for a live checkpoint must have been retrieved at or before the
# checkpoint and be no older than this. There is no silent fallback past
# it: exceeding the tolerance blocks roster-dependent persistence.
LIVE_ROSTER_MAX_AGE_HOURS = 36


@dataclass(frozen=True)
class RosterDataError:
    category: RosterErrorCategory
    message: str


T = TypeVar("T")


@dataclass(frozen=True)
class RosterFetchResult(Generic[T]):
    payload: T | None
    error: RosterDataError | None
    call_metadata: ProviderCallMetadata

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


@dataclass(frozen=True)
class RosterEntry:
    stable_id: str            # GSIS
    display_name: str
    team: CanonicalTeam
    position: str | None
    season: int
    week: int | None


@dataclass(frozen=True)
class RosterSnapshot:
    provider: str
    season: int
    week: int | None
    basis: RosterBasis
    entries: tuple[RosterEntry, ...]
    retrieved_at: datetime

    def age_hours(self, *, at: datetime) -> float:
        return (at - self.retrieved_at).total_seconds() / 3600.0


class RosterDataProvider(Protocol):
    """Two explicit methods rather than one `fetch_roster(season, week)`.

    The live and historical products carry different evidentiary weight:
    a current roster we capture ourselves near a checkpoint IS
    contemporaneous evidence, while the weekly historical dataset is a
    reconstruction whose contemporaneity we did not establish. A single
    method whose meaning changes with its arguments would let a future
    caller reach for the retrospective dataset by accident -- the same
    reasoning that split fetch_quotes from fetch_quotes_as_of.
    """

    provider_name: str

    def fetch_current_roster(self, *, season: int) -> RosterFetchResult[RosterSnapshot]: ...

    def fetch_weekly_roster(
        self, *, season: int, week: int
    ) -> RosterFetchResult[RosterSnapshot]: ...


@dataclass(frozen=True)
class PlayerResolution:
    """One attempt to say who an odds display name actually is."""

    outcome: ResolutionOutcome
    stable_id: str | None = None
    team: CanonicalTeam | None = None
    position: str | None = None
    opponent: CanonicalTeam | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome == "RESOLVED"
