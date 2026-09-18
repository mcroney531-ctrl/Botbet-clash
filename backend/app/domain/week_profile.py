"""What "rehearsal week" and "competitive week" mean in persisted state. Pure.

`Week` carries three independent booleans — `is_real_money`,
`counts_toward_standings`, `counts_toward_awards` — and the preparation CLI
asked only about the first while the other two defaulted True. So
`--no-real-money` could create a week with no money on it that still counted
toward standings AND awards, which is not a rehearsal under any reading of
the governing documents. Three free booleans is eight combinations, of
which the documents describe exactly two.

The mapping is READ FROM the documents, not invented here:

    CONSTITUTION.md §6   Week 0 carries "no real wagers / no official
                         bankroll results / no standings / no season awards"
    RULES.md §3          is_real_money = false
                         counts_toward_standings = false
                         counts_toward_awards = false

and a competitive week is the complement — §6 frames Week 0 as the thing
that happens "before real bankroll competition begins", so a week that IS
the competition carries all three.

The three columns stay the durable facts; a profile is the operator-safe
way to select a combination someone reviewed. A row whose flags match no
profile is reported as NONSTANDARD rather than guessed at, because a
half-rehearsal is exactly the state this exists to make impossible to
create by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WeekProfile(StrEnum):
    COMPETITIVE = "COMPETITIVE"
    """Real money, counts toward standings and toward season awards."""

    REHEARSAL = "REHEARSAL"
    """The Week-0 shape: the full lifecycle runs and none of it counts."""


@dataclass(frozen=True, slots=True)
class WeekFlags:
    is_real_money: bool
    counts_toward_standings: bool
    counts_toward_awards: bool

    def describe(self) -> str:
        return (
            f"is_real_money={self.is_real_money}, "
            f"counts_toward_standings={self.counts_toward_standings}, "
            f"counts_toward_awards={self.counts_toward_awards}"
        )


WEEK_PROFILES: dict[WeekProfile, WeekFlags] = {
    WeekProfile.COMPETITIVE: WeekFlags(True, True, True),
    WeekProfile.REHEARSAL: WeekFlags(False, False, False),
}


def flags_for(profile: WeekProfile) -> WeekFlags:
    return WEEK_PROFILES[profile]


def profile_of(flags: WeekFlags) -> WeekProfile | None:
    """Which reviewed profile these flags are, or None for NONSTANDARD.

    Deliberately returns None rather than a best guess. "No real money but
    it still counts toward awards" is not a rehearsal with a typo; it is a
    state nobody approved, and naming it after the nearest profile would
    hide that.
    """

    for profile, known in WEEK_PROFILES.items():
        if known == flags:
            return profile
    return None
