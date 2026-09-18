"""Create a week PENDING, without opening the competition. Read-mostly, free.

Two different events that used to be one call. `SeasonCommissioner.
open_week` creates the row already OPENED and emits `WEEK_OPENED`, which is
a statement that the competition week has begun. But
`BenchmarkSlatePlan.week_id` needs a Week row BEFORE the first OPENING
checkpoint window, because the slate is precommitted — so getting the FK
target meant declaring the competition open first, purely as a side effect
of the schema.

Preparing a week creates the row in `PENDING` with `opened_at` NULL and
does nothing else: no `CompetitionEvent`, no bankroll transaction, no
competitor state, no provider call, no `Game`. Opening later is a
transition on the row rather than its creation.

Dry run by default. `--apply` required.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select

from app.db.models.season import Season, Week
from app.db.repositories.season_repository import (
    WeekConfigurationConflict,
    week_flags,
)
from app.db.session import session_scope
from app.domain.week_profile import WeekFlags, WeekProfile, flags_for, profile_of
from app.services.season_commissioner import SeasonCommissioner


class WeekPreparationRefused(RuntimeError):
    """Nothing was written."""


@dataclass
class WeekPreparation:
    season_id: uuid.UUID
    season_name: str
    week_number: int
    profile: WeekProfile
    existing_id: uuid.UUID | None = None
    existing_status: str | None = None
    existing_flags: WeekFlags | None = None
    applied: bool = False
    week_id: uuid.UUID | None = None

    @property
    def flags(self) -> WeekFlags:
        return flags_for(self.profile)

    @property
    def already_present(self) -> bool:
        return self.existing_id is not None

    def render(self) -> str:
        out = [
            "=" * 74,
            "WEEK PREPARATION" + ("  (APPLIED)" if self.applied else "  (DRY RUN)"),
            "=" * 74,
            f"  season          {self.season_name}  ({self.season_id})",
            f"  week number     {self.week_number}",
            f"  mode            {self.profile}",
            "",
            "  --- the three durable flags this freezes -----------------------",
            f"    is_real_money              {self.flags.is_real_money}",
            f"    counts_toward_standings    {self.flags.counts_toward_standings}",
            f"    counts_toward_awards       {self.flags.counts_toward_awards}",
            "",
        ]
        if self.already_present:
            existing_profile = profile_of(self.existing_flags) or "NONSTANDARD"
            out += [
                f"  EXISTS ALREADY  {self.existing_id}",
                f"    status        {self.existing_status}",
                f"    mode          {existing_profile}",
                f"    flags         {self.existing_flags.describe()}",
                "",
                "  Nothing to do. Preparation is idempotent only when ALL three",
                "  flags match, so this is the same week, not merely the same",
                "  number.",
            ]
        else:
            out += [
                "  WOULD CREATE    status PENDING, opened_at NULL",
                "",
                "  This does NOT open the competition week. No WEEK_OPENED event,",
                "  no bankroll transaction, no competitor state, no provider call.",
                "  It creates the row a precommitted benchmark slate points at.",
            ]
        out.append("=" * 74)
        return "\n".join(out)


def plan_preparation(
    *, season_id: uuid.UUID, week_number: int, profile: WeekProfile
) -> WeekPreparation:
    # Week 0 is allowed here, unlike in registration. CONSTITUTION.md §6 and
    # RULES.md §3 define a formal Week 0 at `week_number = 0`, so refusing it
    # would make the documented rehearsal week unpreparable. Registration
    # keeps its own >= 1 guard, because a Game must belong to a real NFL week.
    if week_number < 0:
        raise WeekPreparationRefused(
            f"--week-number cannot be negative, got {week_number}"
        )
    with session_scope() as session:
        season = session.get(Season, season_id)
        if season is None:
            raise WeekPreparationRefused(f"season {season_id} not found")
        plan = WeekPreparation(
            season_id=season_id, season_name=season.name,
            week_number=week_number, profile=profile,
        )
        existing = session.execute(
            select(Week).where(
                Week.season_id == season_id, Week.week_number == week_number
            )
        ).scalar_one_or_none()
        if existing is not None:
            plan.existing_id = existing.id
            plan.existing_status = existing.status
            plan.existing_flags = week_flags(existing)
            if plan.existing_flags != plan.flags:
                raise WeekPreparationRefused(
                    f"week {week_number} already exists as "
                    f"{profile_of(plan.existing_flags) or 'NONSTANDARD'}\n"
                    f"    existing:  {plan.existing_flags.describe()}\n"
                    f"    requested: {plan.flags.describe()}\n"
                    "  These flags decide whether the week counts toward the "
                    "season. They are frozen together and not adjusted in place."
                )
        return plan


def apply_preparation(
    *, season_id: uuid.UUID, week_number: int, profile: WeekProfile
) -> WeekPreparation:
    plan = plan_preparation(
        season_id=season_id, week_number=week_number, profile=profile
    )
    commissioner = SeasonCommissioner(season_id=season_id)
    try:
        week_id = commissioner.prepare_week(
            week_number=week_number, profile=profile
        )
    except WeekConfigurationConflict as exc:
        raise WeekPreparationRefused(str(exc)) from exc
    plan.week_id = uuid.UUID(week_id)
    plan.applied = True
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a week PENDING so a precommitted benchmark slate "
                    "has a week to point at. Does NOT open the competition.",
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--week-number", required=True, type=int)
    parser.add_argument(
        "--mode", required=True, choices=[p.value.lower() for p in WeekProfile],
        help="which reviewed week profile to freeze. A MODE rather than three "
             "loose booleans: --no-real-money used to leave a week that still "
             "counted toward standings and awards, which is not a rehearsal "
             "under RULES.md §3 or CONSTITUTION.md §6. Not adjustable "
             "afterwards.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually create the row. WITHOUT this flag nothing is written.",
    )
    args = parser.parse_args(argv)

    try:
        profile = WeekProfile(args.mode.upper())
        if args.apply:
            plan = apply_preparation(
                season_id=args.season_id, week_number=args.week_number,
                profile=profile,
            )
        else:
            plan = plan_preparation(
                season_id=args.season_id, week_number=args.week_number,
                profile=profile,
            )
    except WeekPreparationRefused as exc:
        print("REFUSED — nothing written.")
        print()
        print(f"  {exc}")
        return 1

    print(plan.render())
    if plan.applied:
        print()
        print(f"APPLIED. Week row {plan.week_id} created PENDING.")
        return 0
    print()
    print("DRY RUN — nothing written. Re-run with --apply to create it.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
