"""ONE canonical identity for a scheduled fixture. Pure — no session, no I/O.

`BenchmarkSlot` allocation used to order fixtures by
`(kickoff_at, str(Game.id))`, and `Game.id` is `uuid.uuid4()`. That makes
the selected slate deterministic *inside one database* and unreproducible
from the underlying sports data: rebuild the database from the same
schedule and different UUIDs produce a different benchmark sample. A
precommitted research sample that cannot be regenerated from its inputs is
not precommitted in any meaningful sense.

It was also coupled to the wrong thing. A `Game` row exists only once THE
ODDS API has posted the event, so the market provider's posting horizon
silently decided the benchmark pool — which is how two unlisted Week-3
events could change a slate that nflverse already knew all sixteen
fixtures for.

So identity comes from the authoritative schedule, and from these five
facts only:

    season  game_type  week  canonical away  canonical home

**Kickoff is deliberately NOT part of identity.** Broadcast times move;
the fixture does not. A flexed game is the same fixture at a new time, and
an identity that changed with the clock would silently un-bind a
committed plan. Kickoff travels alongside as metadata, and a disagreement
about it is reported as drift rather than resolved by inventing a second
fixture.

The ordered `(away, home)` pair is the same rule `resolve_fixture` uses,
for the same reason: division rivals meet twice a season but once at each
venue, so the ordered pair identifies a fixture within a season while the
unordered pair would collapse both meetings.

Every service that needs to say "which fixture is this" imports from here.
Two services each formatting their own key string would be two identity
rules wearing one name, and they would agree right up until the week they
mattered.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.rosterdata.teams import CanonicalTeam
from app.scheduledata.base import ScheduledGame, ScheduleSnapshot

FIXTURE_KEY_VERSION = "fixture-key-v1"
"""Stamped anywhere a key is persisted or hashed.

`STABLE_HASH` allocation ranks fixtures by a digest of the key string, so
the key's FORMAT is methodology: changing how a key is spelled would
reshuffle a hash-ranked slate without anyone touching the allocator. A
format change therefore needs a new version and a new allocation-method
name, not a silent edit.
"""

REGULAR_SEASON = "REG"

_KEY_RE = re.compile(r"^(\d{4}):([A-Z]+):W(\d{2}):([A-Z]{2,3})@([A-Z]{2,3})$")


class FixtureKeyError(ValueError):
    """A key that cannot be parsed, or facts that cannot form one."""


@dataclass(frozen=True, slots=True, order=True)
class FixtureKey:
    """`2026:REG:W03:ATL@GB` — sortable, parseable, database-free."""

    season: int
    game_type: str
    week: int
    away: CanonicalTeam
    home: CanonicalTeam

    def __post_init__(self) -> None:
        if self.week < 1:
            raise FixtureKeyError(f"week must be a real NFL week, got {self.week}")
        if self.away is self.home:
            raise FixtureKeyError(f"{self.away.value} cannot host itself")

    @property
    def value(self) -> str:
        """The canonical string. Week is ZERO-PADDED so a plain
        lexicographic sort of keys is also the correct numeric order —
        without it week 10 would sort before week 2 and every ordering
        built on keys would be quietly wrong past week 9."""

        return (
            f"{self.season}:{self.game_type}:W{self.week:02d}:"
            f"{self.away.value}@{self.home.value}"
        )

    @property
    def pair(self) -> str:
        """The WEEK-FREE identity, `2026:REG:ATL@GB`.

        What `resolve_fixture` matches on: it is searching *for* the week,
        so it cannot know it yet. Same five facts minus the one being
        resolved, which keeps the two from drifting apart.
        """

        return f"{self.season}:{self.game_type}:{self.away.value}@{self.home.value}"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value

    @classmethod
    def from_scheduled(cls, game: ScheduledGame) -> FixtureKey:
        return cls(
            season=game.season, game_type=game.game_type, week=game.week,
            away=game.away, home=game.home,
        )

    @classmethod
    def parse(cls, value: str) -> FixtureKey:
        match = _KEY_RE.match(value)
        if match is None:
            raise FixtureKeyError(f"not a fixture key: {value!r}")
        season, game_type, week, away, home = match.groups()
        try:
            return cls(
                season=int(season), game_type=game_type, week=int(week),
                away=CanonicalTeam(away), home=CanonicalTeam(home),
            )
        except ValueError as exc:
            raise FixtureKeyError(f"not a fixture key: {value!r} ({exc})") from exc


@dataclass(frozen=True, slots=True)
class PlannedFixture:
    """A fixture as the allocator sees it: identity plus its kickoff.

    Carries no `Game`, no database id and no provider event ref. Those
    arrive later, if the market provider ever posts the event, and their
    absence must not be able to change which fixtures were planned.
    """

    key: FixtureKey
    kickoff_at: datetime | None

    @property
    def week(self) -> int:
        return self.key.week

    @property
    def label(self) -> str:
        return f"{self.key.away.value} @ {self.key.home.value}"


class IncompletePool(ValueError):
    """A fixture in the target week has no usable kickoff.

    Fatal for an official commitment rather than skipped. The commit
    deadline is the earliest OPENING window start across the whole week,
    and OPENING is computed from kickoff — so one unknown kickoff means
    the deadline itself is unknown, and a deadline you cannot compute
    cannot be enforced. Refusing is the only honest option.
    """


def planned_pool(
    schedule: ScheduleSnapshot,
    *,
    week: int,
    require_kickoffs: bool = True,
) -> tuple[PlannedFixture, ...]:
    """Every regular-season fixture in `week`, in CANONICAL KEY ORDER.

    Key order, not kickoff order: this is the order the pool fingerprint
    is taken over, and it must not move when a broadcast time does. An
    allocator that wants kickoff order sorts for itself.
    """

    fixtures = [
        PlannedFixture(key=FixtureKey.from_scheduled(game), kickoff_at=game.kickoff_at)
        for game in schedule.regular_season()
        if game.week == week
    ]
    if require_kickoffs:
        missing = [f.label for f in fixtures if f.kickoff_at is None]
        if missing:
            raise IncompletePool(
                f"{len(missing)} week-{week} fixture(s) have no kickoff time "
                f"({', '.join(sorted(missing))}). The commit deadline is the "
                "earliest OPENING start across the week and OPENING is computed "
                "from kickoff, so an unknown kickoff makes the deadline "
                "uncomputable. Refusing rather than committing against a "
                "deadline that cannot be checked."
            )
    duplicates = _duplicate_keys(f.key for f in fixtures)
    if duplicates:
        raise FixtureKeyError(
            f"the schedule lists the same fixture twice: {', '.join(duplicates)}. "
            "An ordered pair should be unique within a season and week."
        )
    return tuple(sorted(fixtures, key=lambda f: f.key.value))


def _duplicate_keys(keys: Iterable[FixtureKey]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key in keys:
        if key.value in seen:
            duplicates.add(key.value)
        seen.add(key.value)
    return sorted(duplicates)


def pool_fingerprint(fixtures: Sequence[PlannedFixture]) -> str:
    """A digest of the exact fixture universe an allocator saw.

    Over the canonical KEY strings only — never database ids, never
    kickoff times, never the order the schedule happened to list them in.
    That is what makes it survive a clean rebuild: the same schedule
    produces the same fingerprint on a database whose every UUID is new.

    Version-prefixed so a key-format change cannot silently produce the
    same digest for a differently-spelled pool.
    """

    keys = sorted(f.key.value for f in fixtures)
    body = "\n".join([FIXTURE_KEY_VERSION, *keys])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
