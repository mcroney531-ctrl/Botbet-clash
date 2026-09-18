"""Provider-neutral NFL schedule DTOs.

A schedule answers exactly one question for registration: **which NFL week
does this provider event belong to?**

It exists because the first version of `game_registration` answered that
question with "whichever week the operator typed", which is not an answer
at all. `Game.week_number` is part of a permanent identity scope, and the
Phase 4A.2 acceptance run had already recorded DET @ BUF as week 3 when
the schedule says week 2 — so this is not a hypothetical failure mode.

Deliberately narrow. A schedule provider supplies season, week, the two
canonical teams and a kickoff. It does not supply scores, odds, or
anything a forecast could be built from; a registration pass must not be
able to become a data source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from app.rosterdata.teams import CanonicalTeam

T = TypeVar("T")


@dataclass(frozen=True)
class ScheduleDataError:
    category: str
    message: str


@dataclass(frozen=True)
class ScheduleFetchResult(Generic[T]):
    payload: T | None
    error: ScheduleDataError | None
    call_metadata: object

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


@dataclass(frozen=True)
class ScheduledGame:
    season: int
    week: int
    game_type: str
    home: CanonicalTeam
    away: CanonicalTeam
    kickoff_at: datetime | None

    @property
    def ordered_pair(self) -> tuple[CanonicalTeam, CanonicalTeam]:
        """(away, home). Unique within a season: division rivals meet
        twice, but once at each venue, so the ORDERED pair identifies the
        fixture while the unordered pair would not."""

        return (self.away, self.home)


@dataclass(frozen=True)
class ScheduleSnapshot:
    provider: str
    season: int
    retrieved_at: datetime
    games: tuple[ScheduledGame, ...]

    def regular_season(self) -> tuple[ScheduledGame, ...]:
        return tuple(g for g in self.games if g.game_type == "REG")


class ScheduleProvider(Protocol):
    provider_name: str

    def fetch_schedule(self, *, season: int) -> ScheduleFetchResult[ScheduleSnapshot]: ...
