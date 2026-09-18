"""Is week N ready for competitive operation? (read-only, zero credits)

The state that matters before a week goes live is spread across four
places — the authoritative schedule, the benchmark plan, the `Game` rows
the odds provider has posted, and the checkpoint clock — and answering
"are we ready" by checking them one at a time is how you discover at
16:53 that the first OPENING window opens in seven hours.

So it is one report, and it is deliberately blunt about the shape that
actually occurred:

    schedule fixtures       16/16
    plan                    not committed
    Odds Games registered   0/16
    earliest OPENING        2026-09-25T00:15:00+00:00
    provider listing        incomplete (14/16)

Read-only and free: it reads rules, plan, fixture, Game and CheckpointRun
rows and fetches the free schedule. It never touches the odds provider,
never writes, and never commits anything.

A committed plan's OWN fixture pool is authoritative for that week rather
than a fresh schedule fetch. The plan froze what the allocator saw; if a
later release disagrees, that is schedule drift to report, not a reason to
recompute history.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from app.db.models.forecast_lab import (
    BenchmarkSlateFixture,
    BenchmarkSlatePlan,
    BenchmarkSlot,
)
from app.db.models.markets import CheckpointRun, Game
from app.db.models.season import Week
from app.db.repositories.season_repository import week_flags
from app.db.session import session_scope
from app.domain.week_profile import WeekFlags, profile_of
from app.forecast_lab.checkpoint_window import compute_window
from app.forecast_lab.fixture_identity import FixtureKey, planned_pool
from app.marketdata.game_registration import (
    ScheduleSourceUnavailable,
    _schedule_provider_for,
    _season_pins,
)

OPENING = "OPENING"


class ReadinessUnavailable(RuntimeError):
    """Nothing could be read that answers the question."""


@dataclass
class FixtureReadiness:
    fixture_key: str
    kickoff_at: datetime
    registered: bool
    has_slot: bool
    bound: bool
    opening_at: datetime | None = None
    checkpoints: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        key = FixtureKey.parse(self.fixture_key)
        return f"{key.away.value} @ {key.home.value}"


@dataclass
class WeekReadiness:
    season_id: uuid.UUID
    week_number: int
    now: datetime
    season_name: str | None = None
    rules_version: str | None = None
    allocation_method: str | None = None
    slate_size: int | None = None
    schedule_provider: str | None = None
    schedule_fixture_count: int = 0
    plan_id: uuid.UUID | None = None
    plan_committed_at: datetime | None = None
    plan_is_official: bool = False
    plan_fingerprint: str | None = None
    plan_pool_count: int | None = None
    slot_count: int = 0
    week_id: uuid.UUID | None = None
    week_status: str | None = None
    week_flags: WeekFlags | None = None
    week_opened_at: datetime | None = None

    @property
    def week_profile(self) -> str:
        """The reviewed profile these flags are, or NONSTANDARD.

        Never a best guess. "No real money but it still counts toward
        awards" is not a rehearsal with a typo; it is a combination nobody
        approved, and naming it after the nearest profile would hide that.
        """

        if self.week_flags is None:
            return "—"
        return str(profile_of(self.week_flags) or "NONSTANDARD")
    fixtures: list[FixtureReadiness] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def registered_count(self) -> int:
        return sum(1 for f in self.fixtures if f.registered)

    @property
    def slotted(self) -> list[FixtureReadiness]:
        return [f for f in self.fixtures if f.has_slot]

    @property
    def unbound_slots(self) -> list[FixtureReadiness]:
        return [f for f in self.slotted if not f.bound]

    @property
    def earliest_opening_at(self) -> datetime | None:
        openings = [f.opening_at for f in self.fixtures if f.opening_at]
        return min(openings) if openings else None

    @property
    def plan_committed(self) -> bool:
        return self.plan_id is not None

    @property
    def week_present(self) -> bool:
        return self.week_id is not None

    @property
    def past_commit_deadline(self) -> bool:
        earliest = self.earliest_opening_at
        return earliest is not None and self.now >= earliest

    def render(self) -> str:
        out = [
            "=" * 78,
            f"WEEK READINESS — week {self.week_number}  (read-only, no credits)",
            "=" * 78,
            f"  season                  {self.season_name}  ({self.season_id})",
            f"  rules version           {self.rules_version}",
            f"  allocation method       {self.allocation_method or 'NOT FROZEN'}",
            f"  now                     {self.now.isoformat()}",
            "",
            f"  week row                {'PRESENT' if self.week_present else 'ABSENT'}",
        ]
        if self.week_present:
            out += [
                f"  week id                 {self.week_id}",
                f"  week status             {self.week_status}",
                f"  week mode               {self.week_profile}",
                f"  is_real_money           {self.week_flags.is_real_money}",
                f"  counts_toward_standings {self.week_flags.counts_toward_standings}",
                f"  counts_toward_awards    {self.week_flags.counts_toward_awards}",
                f"  opened_at               "
                f"{self.week_opened_at.isoformat() if self.week_opened_at else '—'}",
            ]
        out += [
            "",
            f"  schedule fixtures       {self.schedule_fixture_count}/"
            f"{self.schedule_fixture_count}   ({self.schedule_provider})",
        ]
        if self.plan_committed:
            out.append(
                f"  plan                    committed "
                f"{self.plan_committed_at.isoformat()}"
                + ("  OFFICIAL" if self.plan_is_official else "  (synthetic)")
            )
            out.append(f"  plan pool               {self.plan_pool_count} fixtures, "
                       f"fingerprint {(self.plan_fingerprint or '')[:16]}…")
            out.append(f"  slots                   {self.slot_count}")
        else:
            out.append("  plan                    NOT COMMITTED")
            out.append(f"  slate size (frozen)     {self.slate_size}")
        total = len(self.fixtures)
        out.append(f"  Odds Games registered   {self.registered_count}/{total}")
        listing = (
            "complete" if self.registered_count == total
            else f"incomplete ({self.registered_count}/{total})"
        )
        out.append(f"  provider listing        {listing}")
        earliest = self.earliest_opening_at
        out.append(
            f"  earliest OPENING        {earliest.isoformat() if earliest else 'unknown'}"
        )
        if earliest is not None:
            delta = (earliest - self.now).total_seconds() / 3600
            out.append(
                f"                          {delta:+.2f}h from now"
                + ("   COMMIT DEADLINE PASSED" if delta <= 0 else "")
            )
        for note in self.notes:
            out.append(f"  NOTE                    {note}")

        out += ["", "  --- verdict ------------------------------------------------------"]
        for line in self._verdict():
            out.append(f"    {line}")

        out += ["", "  --- per fixture ---------------------------------------------------"]
        for fixture in sorted(self.fixtures, key=lambda f: (f.kickoff_at, f.fixture_key)):
            flags = []
            flags.append("SLOT" if fixture.has_slot else "    ")
            flags.append("registered" if fixture.registered else "UNREGISTERED")
            if fixture.has_slot and not fixture.bound:
                flags.append("slot UNBOUND")
            runs = ", ".join(f"{k}={v}" for k, v in sorted(fixture.checkpoints.items()))
            out.append(
                f"    {fixture.label:12} {fixture.kickoff_at.isoformat()}  "
                f"{'  '.join(flags)}" + (f"   [{runs}]" if runs else "")
            )
        out.append("=" * 78)
        return "\n".join(out)

    def _verdict(self) -> list[str]:
        lines: list[str] = []
        # The week row is reported FIRST and explicitly. It was queried
        # before and never surfaced, so a week with no row looked
        # indistinguishable from one merely awaiting its plan -- and
        # official commitment refuses without it.
        if not self.week_present:
            lines.append(
                f"NO WEEK ROW for week {self.week_number}. Official benchmark "
                "commitment cannot occur until the week is PREPARED."
            )
            lines.append(
                "Preparing a week creates it PENDING; it does not open the "
                "competition. See app.services.prepare_week."
            )
        elif self.week_profile == "NONSTANDARD":
            lines.append(
                f"Week row {self.week_id} has a NONSTANDARD profile "
                f"({self.week_flags.describe()}). It matches no reviewed week "
                "mode; a week that is half rehearsal and half competitive is "
                "not a state anyone approved."
            )
        elif self.week_status == "PENDING":
            lines.append(
                f"Week row {self.week_id} is PENDING {self.week_profile} — "
                "prepared, not opened. A slate may be committed against it."
            )
        elif self.week_status == "OPENED":
            lines.append(
                f"Week row {self.week_id} is OPENED {self.week_profile}"
                + (f" at {self.week_opened_at.isoformat()}" if self.week_opened_at else "")
                + " — the competition week has begun."
            )
        else:
            lines.append(f"Week row {self.week_id} is {self.week_status}.")

        if not self.plan_committed:
            if self.past_commit_deadline:
                lines.append(
                    "NOT ELIGIBLE for a first official plan: the earliest OPENING "
                    "window has already opened."
                )
                lines.append(
                    "A benchmark slate is precommitted or it is not a benchmark. "
                    "This week can still be operated as rehearsal."
                )
            else:
                lines.append("No plan committed. The commit window is still open.")
        else:
            lines.append(f"Plan {self.plan_id} committed.")
            if self.unbound_slots:
                lines.append(
                    f"{len(self.unbound_slots)} slotted fixture(s) have no registered "
                    "Game yet:"
                )
                for fixture in self.unbound_slots:
                    lines.append(f"  {fixture.label}  kickoff {fixture.kickoff_at.isoformat()}")
                lines.append(
                    "A slot whose fixture is never listed is a COVERAGE FAILURE. "
                    "It is never reallocated and never substituted."
                )
            else:
                lines.append("Every slotted fixture is bound to a registered Game.")
        if self.registered_count < len(self.fixtures):
            lines.append(
                f"{len(self.fixtures) - self.registered_count} fixture(s) are not "
                "registered. Registration is idempotent and can be re-run; a "
                "committed plan does not change when they arrive."
            )
        return lines


def assess_week(
    *,
    season_id: uuid.UUID,
    week_number: int,
    schedule_provider=None,
    now: datetime | None = None,
) -> WeekReadiness:
    moment = now or datetime.now(timezone.utc)
    report = WeekReadiness(season_id=season_id, week_number=week_number, now=moment)

    with session_scope() as session:
        season, rules = _season_pins(session, season_id)
        report.season_name = season.name
        report.rules_version = rules.rules_version
        report.allocation_method = rules.benchmark_allocation_method
        report.slate_size = rules.benchmark_slate_size
        windows = dict(rules.checkpoint_windows or {})
        season_year = season.year
        roster_pin = rules.roster_data_provider

        week = session.execute(
            select(Week).where(
                Week.season_id == season_id, Week.week_number == week_number
            )
        ).scalar_one_or_none()
        plan = None
        if week is not None:
            report.week_id = week.id
            report.week_status = week.status
            report.week_flags = week_flags(week)
            report.week_opened_at = week.opened_at
            plan = session.execute(
                select(BenchmarkSlatePlan).where(BenchmarkSlatePlan.week_id == week.id)
            ).scalar_one_or_none()

        planned_rows: list[BenchmarkSlateFixture] = []
        slotted_fixture_ids: set[uuid.UUID] = set()
        if plan is not None:
            report.plan_id = plan.id
            report.plan_committed_at = plan.committed_at
            report.plan_is_official = plan.is_official
            report.plan_fingerprint = plan.fixture_pool_fingerprint
            report.plan_pool_count = plan.fixture_pool_count
            planned_rows = list(session.execute(
                select(BenchmarkSlateFixture)
                .where(BenchmarkSlateFixture.plan_id == plan.id)
            ).scalars())
            slots = list(session.execute(
                select(BenchmarkSlot).where(BenchmarkSlot.plan_id == plan.id)
            ).scalars())
            report.slot_count = len(slots)
            slotted_fixture_ids = {
                s.slate_fixture_id for s in slots if s.slate_fixture_id
            }

        games = {
            (g.away_team_canonical, g.home_team_canonical): g
            for g in session.execute(
                select(Game).where(
                    Game.season_id == season_id, Game.week_number == week_number
                )
            ).scalars()
        }
        runs: dict[uuid.UUID, dict[str, str]] = {}
        for run in session.execute(
            select(CheckpointRun).where(
                CheckpointRun.game_id.in_([g.id for g in games.values()] or [uuid.uuid4()])
            )
        ).scalars():
            runs.setdefault(run.game_id, {})[run.checkpoint_type] = run.status

        if plan is not None:
            # The PLAN's own frozen pool is authoritative for a committed
            # week. A fresh fetch could disagree; that is drift to report,
            # not grounds to recompute what was already committed.
            for row in planned_rows:
                key = FixtureKey.parse(row.fixture_key)
                game = games.get((key.away.value, key.home.value))
                report.fixtures.append(FixtureReadiness(
                    fixture_key=row.fixture_key,
                    kickoff_at=row.planned_kickoff_at,
                    registered=game is not None,
                    has_slot=row.id in slotted_fixture_ids,
                    bound=row.game_id is not None,
                    checkpoints=runs.get(game.id, {}) if game else {},
                ))
            report.schedule_fixture_count = len(planned_rows)
            report.notes.append(
                "fixtures listed from the COMMITTED PLAN's frozen pool, not a "
                "fresh schedule fetch"
            )

    if plan is None:
        try:
            source = schedule_provider or _schedule_provider_for(roster_pin)
            report.schedule_provider = getattr(source, "provider_name", "?")
            result = source.fetch_schedule(season=season_year)
        except ScheduleSourceUnavailable as exc:
            raise ReadinessUnavailable(str(exc)) from exc
        if not result.ok:
            raise ReadinessUnavailable(
                f"schedule unavailable ({result.error.category}: {result.error.message})"
            )
        pool = planned_pool(result.payload, week=week_number, require_kickoffs=False)
        report.schedule_fixture_count = len(pool)
        with session_scope() as session:
            for fixture in pool:
                game = games.get((fixture.key.away.value, fixture.key.home.value))
                report.fixtures.append(FixtureReadiness(
                    fixture_key=fixture.key.value,
                    kickoff_at=fixture.kickoff_at,
                    registered=game is not None,
                    has_slot=False,
                    bound=False,
                    checkpoints=runs.get(game.id, {}) if game else {},
                ))
    else:
        report.schedule_provider = "(committed plan)"

    if OPENING in windows:
        for fixture in report.fixtures:
            if fixture.kickoff_at is not None:
                fixture.opening_at = compute_window(
                    fixture.kickoff_at, OPENING, windows
                ).window_start
    else:
        report.notes.append("no OPENING window frozen; deadlines cannot be computed")
    return report


def main(argv: Sequence[str] | None = None, *, schedule_provider=None) -> int:
    parser = argparse.ArgumentParser(
        description="Is week N ready for competitive operation? Read-only, free.",
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--week-number", required=True, type=int)
    args = parser.parse_args(argv)

    try:
        report = assess_week(
            season_id=args.season_id, week_number=args.week_number,
            schedule_provider=schedule_provider,
        )
    except (ReadinessUnavailable, LookupError) as exc:
        print("CANNOT ASSESS — nothing read that answers this.")
        print()
        print(f"  {exc}")
        return 1
    print(report.render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
