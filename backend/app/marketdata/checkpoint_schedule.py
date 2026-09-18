"""When does every checkpoint for a week actually open? (read-only, free)

Two questions kept getting answered with "if the windows are 144/96, then
roughly...". That is a guess about production dressed as arithmetic. The
frozen `checkpoint_windows` live in the season's active `SeasonRules`, and
they are the only thing that decides when a capture may run — so this
reads them and projects the real timestamps.

It exists because of a specific, time-boxed decision. `BenchmarkSlatePlan`
is UNIQUE per week and its own docstring says it is committed *before any
game's OPENING window opens*. That makes the earliest OPENING start a hard
deadline you cannot renegotiate later, and "when is it" should not be a
mental calculation performed under time pressure.

Deliberately zero-cost and read-only:

    MAY   read SeasonRules, Game and CheckpointRun rows
          fetch the FREE schedule to cover fixtures not yet registered
    MUST NOT  call the odds provider, create any row, spend any credit

Fixtures come from two places on purpose. Registered `Game` rows are
authoritative — their `kickoff_at` is what the capture cycle will actually
use. The schedule fills in fixtures that exist in the real world but have
no `Game` row yet, which is how you see that the provider has listed 14 of
16 games *before* committing a slate drawn from an incomplete pool.

The window arithmetic is imported, never reimplemented. A second copy
could disagree with the cycle it is supposed to be predicting, and a
projection you cannot trust is worse than no projection.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from app.db.models.markets import CheckpointRun, Game
from app.db.session import session_scope
from app.forecast_lab.checkpoint_window import (
    CheckpointDisposition,
    CheckpointWindow,
    compute_window,
    disposition,
)
from app.marketdata.game_registration import (
    ScheduleSourceUnavailable,
    _schedule_provider_for,
    _season_pins,
)
from app.rosterdata.teams import CanonicalTeam

CHECKPOINT_ORDER = ("OPENING", "MID", "FINAL")


class ScheduleProjectionFailed(RuntimeError):
    """Nothing was read that could answer the question."""


@dataclass
class FixtureProjection:
    away: str
    home: str
    kickoff_at: datetime
    registered: bool
    game_id: uuid.UUID | None = None
    windows: dict[str, CheckpointWindow] = field(default_factory=dict)
    dispositions: dict[str, CheckpointDisposition] = field(default_factory=dict)
    statuses: dict[str, str | None] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.away} @ {self.home}"

    def opening_start(self) -> datetime | None:
        window = self.windows.get("OPENING")
        return window.window_start if window else None


@dataclass
class WeekProjection:
    season_id: uuid.UUID
    season_name: str | None
    week_number: int
    now: datetime
    rules_version: str | None = None
    windows_config: dict = field(default_factory=dict)
    schedule_provider: str | None = None
    fixtures: list[FixtureProjection] = field(default_factory=list)
    schedule_note: str | None = None

    @property
    def registered(self) -> list[FixtureProjection]:
        return [f for f in self.fixtures if f.registered]

    @property
    def unregistered(self) -> list[FixtureProjection]:
        return [f for f in self.fixtures if not f.registered]

    def slate_deadline(self) -> datetime | None:
        """The earliest OPENING start across the week.

        Computed over EVERY fixture, registered or not. A deadline computed
        only from registered games would move later every time the provider
        was slow to list one -- which is precisely backwards, since the
        unlisted game is the reason to hurry.
        """

        starts = [s for s in (f.opening_start() for f in self.fixtures) if s is not None]
        return min(starts) if starts else None

    def next_window_opening(self) -> tuple[FixtureProjection, str, CheckpointWindow] | None:
        upcoming = [
            (f, name, w)
            for f in self.fixtures
            for name, w in f.windows.items()
            if w.window_start > self.now
        ]
        if not upcoming:
            return None
        return min(upcoming, key=lambda t: t[2].window_start)

    def open_now(self) -> list[tuple[FixtureProjection, str, CheckpointWindow]]:
        return sorted(
            (
                (f, name, w)
                for f in self.fixtures
                for name, w in f.windows.items()
                if w.contains(self.now)
            ),
            key=lambda t: t[2].window_end,
        )


def _delta(now: datetime, when: datetime) -> str:
    seconds = (when - now).total_seconds()
    sign = "in" if seconds >= 0 else "ago"
    seconds = abs(seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [f"{days}d" if days else "", f"{hours}h" if hours or days else "", f"{minutes}m"]
    body = " ".join(p for p in parts if p)
    return f"{sign} {body}" if sign == "in" else f"{body} {sign}"


def project_week(
    *,
    season_id: uuid.UUID,
    week_number: int,
    schedule_provider=None,
    now: datetime | None = None,
) -> WeekProjection:
    """Read the frozen windows and project them over the week's fixtures."""

    moment = now or datetime.now(timezone.utc)

    with session_scope() as session:
        season, rules = _season_pins(session, season_id)
        report = WeekProjection(
            season_id=season_id, season_name=season.name, week_number=week_number,
            now=moment, rules_version=rules.rules_version,
            windows_config=dict(rules.checkpoint_windows),
        )
        season_year = season.year
        roster_pin = rules.roster_data_provider

        games = session.execute(
            select(Game)
            .where(Game.season_id == season_id, Game.week_number == week_number)
            .order_by(Game.kickoff_at)
        ).scalars().all()

        runs: dict[uuid.UUID, dict[str, str]] = {}
        for run in session.execute(
            select(CheckpointRun).where(
                CheckpointRun.game_id.in_([g.id for g in games] or [uuid.uuid4()])
            )
        ).scalars():
            runs.setdefault(run.game_id, {})[run.checkpoint_type] = run.status

        for game in games:
            report.fixtures.append(FixtureProjection(
                away=game.away_team_canonical, home=game.home_team_canonical,
                kickoff_at=game.kickoff_at, registered=True, game_id=game.id,
                statuses={
                    name: runs.get(game.id, {}).get(name) for name in CHECKPOINT_ORDER
                },
            ))

    if not report.windows_config:
        raise ScheduleProjectionFailed(
            f"season {season_id} (rules {report.rules_version}) has no "
            "checkpoint_windows frozen; there is nothing to project."
        )

    # The free schedule, fetched OUTSIDE the transaction. It answers a
    # question the Game rows cannot: which fixtures exist but are not
    # registered yet.
    try:
        source = schedule_provider or _schedule_provider_for(roster_pin)
        report.schedule_provider = getattr(source, "provider_name", "?")
        result = source.fetch_schedule(season=season_year)
    except ScheduleSourceUnavailable as exc:
        report.schedule_note = f"schedule source unavailable: {exc}"
        result = None

    if result is not None and result.ok:
        known = {(f.away, f.home) for f in report.fixtures}
        for scheduled in result.payload.regular_season():
            if scheduled.week != week_number:
                continue
            pair = (scheduled.away.value, scheduled.home.value)
            if pair in known or scheduled.kickoff_at is None:
                continue
            report.fixtures.append(FixtureProjection(
                away=pair[0], home=pair[1], kickoff_at=scheduled.kickoff_at,
                registered=False,
                statuses={name: None for name in CHECKPOINT_ORDER},
            ))
    elif result is not None:
        report.schedule_note = (
            f"schedule fetch failed ({result.error.category}: {result.error.message}); "
            "unregistered fixtures are NOT accounted for below"
        )

    for fixture in report.fixtures:
        for name in CHECKPOINT_ORDER:
            window = compute_window(fixture.kickoff_at, name, report.windows_config)
            fixture.windows[name] = window
            fixture.dispositions[name] = disposition(
                status=fixture.statuses.get(name), window=window, now=moment,
            )

    report.fixtures.sort(key=lambda f: (f.kickoff_at, f.label))
    return report


def render(report: WeekProjection) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 78)
    add(f"CHECKPOINT SCHEDULE — week {report.week_number}  (read-only, no credits)")
    add("=" * 78)
    add(f"  season          {report.season_name}  ({report.season_id})")
    add(f"  rules version   {report.rules_version}  (the FROZEN windows, not a default)")
    add(f"  now             {report.now.isoformat()}")
    add("")
    add("  --- frozen checkpoint_windows ---------------------------------")
    for name in CHECKPOINT_ORDER:
        cfg = report.windows_config.get(name)
        if cfg is None:
            add(f"    {name:8}  MISSING from the frozen policy")
            continue
        add(f"    {name:8}  opens {cfg['start_hours_before_kickoff']}h before kickoff, "
            f"closes {cfg['end_hours_before_kickoff']}h before")
    add("")
    add(f"  fixtures        {len(report.registered)} registered, "
        f"{len(report.unregistered)} scheduled but NOT registered")
    if report.schedule_provider:
        add(f"  schedule        {report.schedule_provider}")
    if report.schedule_note:
        add(f"  NOTE            {report.schedule_note}")

    deadline = report.slate_deadline()
    add("")
    add("  --- the deadline you cannot renegotiate ------------------------")
    if deadline is None:
        add("    no fixtures found for this week")
    else:
        add(f"    earliest OPENING window starts  {deadline.isoformat()}")
        add(f"                                    {_delta(report.now, deadline)}")
        add("")
        add("    BenchmarkSlatePlan.week_id is UNIQUE and the plan is committed")
        add("    once per week, before any game's OPENING window opens. A slate")
        add("    committed while fixtures are still unregistered is drawn from an")
        add("    incomplete pool, permanently.")
        if report.unregistered:
            add("")
            add(f"    {len(report.unregistered)} fixture(s) are NOT registered yet:")
            for fixture in report.unregistered:
                add(f"      {fixture.label:12} kickoff {fixture.kickoff_at.isoformat()}")

    add("")
    add("  --- open right now ---------------------------------------------")
    open_now = report.open_now()
    if not open_now:
        add("    nothing is in a capture window")
    for fixture, name, window in open_now:
        mark = "" if fixture.registered else "   (NOT REGISTERED -- cannot capture)"
        add(f"    {name:8} {fixture.label:12} closes {window.window_end.isoformat()}  "
            f"({_delta(report.now, window.window_end)}){mark}")

    nxt = report.next_window_opening()
    add("")
    add("  --- next window to open ----------------------------------------")
    if nxt is None:
        add("    none upcoming")
    else:
        fixture, name, window = nxt
        add(f"    {name} for {fixture.label} at {window.window_start.isoformat()}")
        add(f"    {_delta(report.now, window.window_start)}")

    add("")
    add("  --- per fixture -------------------------------------------------")
    for fixture in report.fixtures:
        flag = "" if fixture.registered else "  [NOT REGISTERED]"
        add("")
        add(f"    {fixture.label:12} kickoff {fixture.kickoff_at.isoformat()}{flag}")
        for name in CHECKPOINT_ORDER:
            window = fixture.windows.get(name)
            if window is None:
                continue
            state = fixture.dispositions[name]
            status = fixture.statuses.get(name) or "no run"
            add(f"      {name:8} {window.window_start.isoformat()} .. "
                f"{window.window_end.isoformat()}")
            add(f"               target {window.target_time.isoformat()}   "
                f"{state}  ({status})")
    add("=" * 78)
    return "\n".join(out)


def main(argv: Sequence[str] | None = None, *, schedule_provider=None) -> int:
    parser = argparse.ArgumentParser(
        description="Project a week's checkpoint windows from the FROZEN SeasonRules "
                    "(read-only, no provider credits)",
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--week-number", required=True, type=int)
    args = parser.parse_args(argv)

    try:
        report = project_week(
            season_id=args.season_id, week_number=args.week_number,
            schedule_provider=schedule_provider,
        )
    except (ScheduleProjectionFailed, LookupError) as exc:
        print("CANNOT PROJECT — nothing written, nothing read that answers this.")
        print()
        print(f"  {exc}")
        return 1
    print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
