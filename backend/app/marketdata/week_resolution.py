"""Which NFL week does a provider event belong to? (Phase 4A.6 correction)

Pure. No session, no I/O, no provider.

This exists because the first version of `game_registration` answered the
question with "whichever week the operator typed at the CLI". That is not
a verification, it is a relabelling — and `Game.week_number` is part of a
permanent identity scope, so getting it wrong is not something a later
correct run repairs. It fails loudly forever after, on the right data.

The failure was not hypothetical. Run on 2026-09-18 with the arguments
actually suggested (`--week-number 4 --days-ahead 8`), the old code would
have swept a 2026-09-18..09-26 calendar window — containing fifteen Week 2
games and one Week 3 game, and **zero** Week 4 games, since Week 4 runs
2026-10-01..10-05 — and stamped all sixteen as Week 4. The Phase 4A.2
acceptance run had already made the same class of mistake by hand: it
recorded DET @ BUF as week 3 when the schedule says week 2.

Matching rule, deliberately conservative: the ORDERED (away, home) pair.
Division rivals meet twice a season but once at each venue, so the ordered
pair identifies a fixture within a season while the unordered pair would
not. Anything that does not match exactly one scheduled fixture is
refused rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.rosterdata.teams import CanonicalTeam
from app.scheduledata.base import ScheduledGame, ScheduleSnapshot

DEFAULT_KICKOFF_TOLERANCE = timedelta(hours=3)
"""How far a provider kickoff may sit from the scheduled one.

Wide enough to absorb a rounded or provisional broadcast time; narrow
enough that a flex move to another slot or another day is REFUSED rather
than accepted. Refusing is the right outcome there: a disagreement that
large means either the schedule snapshot is stale or the fixture is not
the one we matched, and both deserve a human rather than a permanent row.
"""


class WeekOutcome(StrEnum):
    MATCHED = "MATCHED"
    UNKNOWN_FIXTURE = "UNKNOWN_FIXTURE"
    AMBIGUOUS_FIXTURE = "AMBIGUOUS_FIXTURE"
    OTHER_WEEK = "OTHER_WEEK"
    KICKOFF_DISAGREEMENT = "KICKOFF_DISAGREEMENT"

    @property
    def may_persist(self) -> bool:
        """Only a positive match may become a permanent Game row."""

        return self is WeekOutcome.MATCHED


@dataclass(frozen=True, slots=True)
class WeekResolution:
    outcome: WeekOutcome
    week: int | None
    detail: str
    scheduled_kickoff: datetime | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome.may_persist


def resolve_event_week(
    *,
    home: CanonicalTeam,
    away: CanonicalTeam,
    kickoff_at: datetime,
    requested_week: int,
    schedule: ScheduleSnapshot,
    kickoff_tolerance: timedelta = DEFAULT_KICKOFF_TOLERANCE,
) -> WeekResolution:
    """Resolve, then REQUIRE the resolved week to equal the requested one.

    Never "the operator asked for week 4, so this is week 4". The schedule
    decides what week a fixture is; the request only decides whether we are
    interested in it right now.
    """

    matches: list[ScheduledGame] = [
        g for g in schedule.regular_season() if g.ordered_pair == (away, home)
    ]

    if not matches:
        return WeekResolution(
            outcome=WeekOutcome.UNKNOWN_FIXTURE,
            week=None,
            detail=(
                f"{away.value} @ {home.value} is not a {schedule.season} regular-season "
                f"fixture in the {schedule.provider} schedule"
            ),
        )
    if len(matches) > 1:
        weeks = sorted(g.week for g in matches)
        return WeekResolution(
            outcome=WeekOutcome.AMBIGUOUS_FIXTURE,
            week=None,
            detail=(
                f"{away.value} @ {home.value} matches {len(matches)} scheduled fixtures "
                f"(weeks {weeks}); an ordered pair should be unique within a season"
            ),
        )

    scheduled = matches[0]
    if scheduled.week != requested_week:
        return WeekResolution(
            outcome=WeekOutcome.OTHER_WEEK,
            week=scheduled.week,
            detail=(
                f"{away.value} @ {home.value} is week {scheduled.week}, not the "
                f"requested week {requested_week}"
            ),
            scheduled_kickoff=scheduled.kickoff_at,
        )

    if scheduled.kickoff_at is not None:
        drift = abs(kickoff_at - scheduled.kickoff_at)
        if drift > kickoff_tolerance:
            return WeekResolution(
                outcome=WeekOutcome.KICKOFF_DISAGREEMENT,
                week=scheduled.week,
                detail=(
                    f"provider kickoff {kickoff_at.isoformat()} differs from the "
                    f"scheduled {scheduled.kickoff_at.isoformat()} by "
                    f"{drift.total_seconds() / 3600:.1f}h, beyond the "
                    f"{kickoff_tolerance.total_seconds() / 3600:.0f}h tolerance"
                ),
                scheduled_kickoff=scheduled.kickoff_at,
            )

    return WeekResolution(
        outcome=WeekOutcome.MATCHED,
        week=scheduled.week,
        detail=f"week {scheduled.week} confirmed by the {schedule.provider} schedule",
        scheduled_kickoff=scheduled.kickoff_at,
    )
