"""Event discovery and Game registration — IDENTITY ONLY.

`official_capture` deliberately requires a `Game` to already exist: it
rebuilds the provider event from `Game.external_ref` rather than
rediscovering it, so a capture can never be pointed at the wrong event by
a typo. That correctness property leaves a gap — something has to create
the `Game` rows first — and this is it.

Scope is narrow on purpose. Registration may create and verify `Game`
rows and nothing else:

    MAY    call the provider's events endpoint
           canonicalize home/away teams
           create Game rows
           verify existing Game rows against their full scope

    MUST NOT  fetch player props or roster data
              create Player / GamePlayer
              create PropMarket / PropQuote
              create MarketSnapshot / EvidenceSnapshot
              create CheckpointRun
              call a model

That last group is what `live_ingest` also does, which is why this is not
just "run live_ingest for the week": registration should be repeatable and
free, and mixing it with paid quote ingestion would make it neither.

`/events` costs **zero** credits on this provider (seam doc §16), so a
registration pass can be re-run as often as the schedule changes.
Provider-call provenance is still recorded — a free call is still a call
we made, and the audit chain should not have holes just because a row
happens to cost nothing.

**The NFL week is VERIFIED, never assumed.** The first version of this
module stamped every event in a `now .. now + N days` calendar window with
whatever week the operator typed. Run on 2026-09-18 with the arguments
actually suggested, that would have labelled fifteen Week 2 games and one
Week 3 game as Week 4 — and caught zero real Week 4 games, which run
2026-10-01..10-05. Every event's week now comes from the schedule
provider; the requested week only decides what we are interested in.

**Preview is the default.** Persisting requires an explicit `--apply`. A
discovery mistake must never be able to create an immutable Game row,
because `Game.external_ref` is permanent and a later correct run fails
loudly rather than repairing it.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.markets import Game
from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.marketdata.dto import ProviderEvent
from app.marketdata.providers.the_odds_api import (
    DEFAULT_SPORT,
    PROVIDER_NAME as ODDS_PROVIDER,
    TheOddsApiProvider,
)
from app.marketdata.telemetry import finish_run, record_call, sanitize_message, start_run
from app.marketdata.week_resolution import WeekOutcome, WeekResolution, resolve_event_week
from app.rosterdata.providers.nflverse import NflverseScheduleProvider
from app.rosterdata.teams import CanonicalTeam, TeamMappingError, canonical_from_odds_api


class EventScopeConflict(RuntimeError):
    """An existing Game with this external_ref describes a different game.

    `Game.external_ref` is a PERMANENT identity. Reusing it on the strength
    of the ref alone would mean the poisoned-event protection only ever
    covered the first write; silently relocating the row would hide
    whichever write was wrong. Neither is acceptable, so this is a hard
    stop that a human resolves.
    """


@dataclass
class RegisteredGame:
    game_id: uuid.UUID | None
    external_ref: str
    home: str
    away: str
    kickoff_at: datetime
    created: bool
    week: int | None = None
    # `game_id is None` means PREVIEW: this event resolved to the requested
    # week and would be registered, but nothing was written.
    previewed: bool = False


@dataclass
class RegistrationReport:
    season_id: uuid.UUID
    season_name: str | None = None
    week_number: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    provider: str | None = None
    applied: bool = False
    events_observed: int = 0
    created: list[RegisteredGame] = field(default_factory=list)
    reused: list[RegisteredGame] = field(default_factory=list)
    eligible: list[RegisteredGame] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unmapped_teams: list[str] = field(default_factory=list)
    # Every event the provider returned that does NOT belong to the
    # requested week, with the reason. Reported rather than silently
    # dropped: "we saw 16 events and registered 1" is a fact an operator
    # needs, and it is how the original bug would have been caught.
    refused: list[str] = field(default_factory=list)
    schedule_provider: str | None = None
    quota_cost: int = 0
    quota_remaining: int | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def all_games(self) -> list[RegisteredGame]:
        return sorted(self.created + self.reused, key=lambda g: g.kickoff_at)


def verify_game_scope(
    game: Game,
    *,
    season_id: uuid.UUID,
    week_number: int,
    home: CanonicalTeam,
    away: CanonicalTeam,
    kickoff_at: datetime,
) -> None:
    """Every field, not just the external_ref.

    Checking only the ref would make the protection cover the FIRST write
    and nothing after it: a row attached to the wrong season or week would
    then be found and reused by every later, correct run.
    """

    mismatches = []
    if game.season_id != season_id:
        mismatches.append(f"season_id {game.season_id} != {season_id}")
    if game.week_number != week_number:
        mismatches.append(f"week_number {game.week_number} != {week_number}")
    if game.home_team_canonical != home.value:
        mismatches.append(f"home {game.home_team_canonical} != {home.value}")
    if game.away_team_canonical != away.value:
        mismatches.append(f"away {game.away_team_canonical} != {away.value}")
    if game.kickoff_at != kickoff_at:
        mismatches.append(f"kickoff {game.kickoff_at.isoformat()} != {kickoff_at.isoformat()}")
    if mismatches:
        raise EventScopeConflict(
            f"EVENT_SCOPE_CONFLICT for {game.external_ref}: " + "; ".join(mismatches)
            + ".\nThe existing row is NOT being repaired or moved automatically -- "
            "an external_ref is a permanent identity and silently relocating it "
            "would hide whichever write was wrong."
        )


def register_game(
    session: Session,
    *,
    event: ProviderEvent,
    season_id: uuid.UUID,
    week_number: int,
    home: CanonicalTeam,
    away: CanonicalTeam,
) -> tuple[Game, bool]:
    """Get-or-create one Game by its permanent provider identity.

    ONE implementation, shared with `live_ingest`. Returns (game, created).

    The IntegrityError branch is the concurrency case: two registration
    passes racing both see no row and both INSERT. The unique index on
    `external_ref` decides, and the loser re-reads and verifies the winner's
    row rather than failing — the outcome a caller wants is "this event is
    registered", and it is.
    """

    external_ref = event.ref.as_external_ref()
    existing = session.execute(
        select(Game).where(Game.external_ref == external_ref)
    ).scalar_one_or_none()
    if existing is not None:
        verify_game_scope(
            existing, season_id=season_id, week_number=week_number,
            home=home, away=away, kickoff_at=event.kickoff_at,
        )
        return existing, False

    row = Game(
        external_ref=external_ref,
        season_id=season_id,
        week_number=week_number,
        home_team=event.home_team,
        away_team=event.away_team,
        home_team_canonical=home.value,
        away_team_canonical=away.value,
        kickoff_at=event.kickoff_at,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = session.execute(
            select(Game).where(Game.external_ref == external_ref)
        ).scalar_one()
        verify_game_scope(
            winner, season_id=season_id, week_number=week_number,
            home=home, away=away, kickoff_at=event.kickoff_at,
        )
        return winner, False
    return row, True


def _season_pins(session: Session, season_id: uuid.UUID) -> tuple[Season, SeasonRules]:
    season = session.get(Season, season_id)
    if season is None:
        raise LookupError(f"season {season_id} not found")
    rules = session.execute(
        select(SeasonRules)
        .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
        .order_by(SeasonRules.effective_from.desc())
    ).scalars().first()
    if rules is None:
        raise LookupError(f"season {season_id} has no active SeasonRules")
    return season, rules


def register_week_events(
    *,
    season_id: uuid.UUID,
    week_number: int,
    window_start: datetime,
    window_end: datetime,
    sport: str = DEFAULT_SPORT,
    odds_provider=None,
    schedule_provider=None,
    apply: bool = False,
) -> RegistrationReport:
    """Discover the provider's events in a window, VERIFY their NFL week,
    and — only with `apply=True` — register the ones that belong to the
    requested week.

    The market provider comes from the season's active `SeasonRules`, never
    from an operator flag: a run that used a different feed from the one the
    season is pinned to would create Game rows no capture could refresh.

    The calendar window is a DISCOVERY filter only. It decides which events
    we ask the provider about; it never decides which week they are in.
    Conflating those is the bug this function was rewritten to remove.
    """

    report = RegistrationReport(
        season_id=season_id, week_number=week_number,
        window_start=window_start, window_end=window_end, applied=apply,
    )

    if week_number < 1:
        raise ValueError(
            f"--week-number must be a real NFL week (got {week_number}). Week 0 "
            "would permanently mis-scope every external_ref this run writes."
        )

    with session_scope() as session:
        season, rules = _season_pins(session, season_id)
        report.season_name = season.name
        report.provider = rules.market_data_provider
        season_year = season.year

    if report.provider != ODDS_PROVIDER:
        raise ValueError(
            f"season {season_id} is pinned to {report.provider!r}; no event "
            "registration is implemented for it"
        )

    # Network first, OUTSIDE any transaction -- the same rule the capture
    # cycle follows, for the same reason.
    odds = odds_provider or TheOddsApiProvider()
    schedule_source = schedule_provider or NflverseScheduleProvider()
    report.schedule_provider = getattr(schedule_source, "provider_name", "?")

    # The schedule is fetched FIRST and its failure is fatal. Without an
    # authoritative week there is nothing to verify against, and the only
    # safe behaviour is to register nothing -- which is exactly what the
    # old code did not do.
    schedule_result = schedule_source.fetch_schedule(season=season_year)
    if not schedule_result.ok:
        report.failures.append(
            f"schedule: {schedule_result.error.category}: {schedule_result.error.message}"
        )
        return report
    schedule = schedule_result.payload

    result = odds.list_events(sport=sport, window_start=window_start, window_end=window_end)

    with session_scope() as session:
        run = start_run(session, provider=ODDS_PROVIDER, operation="LIST_EVENTS", sport=sport)
        record_call(
            session, run=run, metadata=result.call_metadata, success=result.ok,
            error_category=result.error.category if result.error else None,
            error_message=result.error.message if result.error else None,
        )
        run_id = run.id
        # Recorded even though /events is free: a free call is still a call
        # we made, and the audit chain should not have holes just because a
        # row happens to cost nothing.
        report.quota_cost += result.call_metadata.quota_cost or 0
        if result.call_metadata.quota_remaining is not None:
            report.quota_remaining = result.call_metadata.quota_remaining

    if not result.ok:
        report.failures.append(
            f"list_events: {result.error.category}: {sanitize_message(result.error.message)}"
        )
        with session_scope() as session:
            from app.db.models.ingestion import IngestionRun

            finish_run(session, run=session.get(IngestionRun, run_id), status="FAILED")
        return report

    events = result.payload or []
    report.events_observed = len(events)

    for event in events:
        try:
            home = canonical_from_odds_api(event.home_team)
            away = canonical_from_odds_api(event.away_team)
        except TeamMappingError as exc:
            report.unmapped_teams.append(f"{event.away_team} @ {event.home_team}: {exc}")
            continue

        # THE WEEK IS VERIFIED, NOT ASSUMED. Anything that does not resolve
        # to exactly the requested week is refused, never relabelled.
        resolution = resolve_event_week(
            home=home, away=away, kickoff_at=event.kickoff_at,
            requested_week=week_number, schedule=schedule,
        )
        if not resolution.resolved:
            report.refused.append(
                f"{away.value} @ {home.value} [{event.ref.external_event_id}]: "
                f"{resolution.outcome} — {resolution.detail}"
            )
            continue

        if not apply:
            # Preview. Nothing is written, so no Game row can be created by
            # a discovery mistake.
            report.eligible.append(RegisteredGame(
                game_id=None, external_ref=event.ref.as_external_ref(),
                home=home.value, away=away.value, kickoff_at=event.kickoff_at,
                created=False, week=resolution.week, previewed=True,
            ))
            continue

        # One transaction per event: a conflict on one game must not roll
        # back the registrations that already succeeded.
        try:
            with session_scope() as session:
                game, created = register_game(
                    session, event=event, season_id=season_id,
                    week_number=resolution.week, home=home, away=away,
                )
                entry = RegisteredGame(
                    game_id=game.id, external_ref=game.external_ref,
                    home=home.value, away=away.value,
                    kickoff_at=game.kickoff_at, created=created,
                    week=game.week_number,
                )
        except EventScopeConflict as exc:
            report.conflicts.append(str(exc))
            continue

        (report.created if entry.created else report.reused).append(entry)

    with session_scope() as session:
        from app.db.models.ingestion import IngestionRun

        finish_run(
            session, run=session.get(IngestionRun, run_id),
            status="SUCCEEDED" if not report.conflicts else "PARTIAL",
        )
    return report


def render(report: RegistrationReport) -> str:
    out: list[str] = []
    add = out.append
    mode = "APPLY — Game rows written" if report.applied else "PREVIEW — nothing written"
    add("=" * 78)
    add(f"WEEK EVENT REGISTRATION — {mode}")
    add("=" * 78)
    add(f"  season            {report.season_name}  ({report.season_id})")
    add(f"  requested week    {report.week_number}")
    add(f"  market provider   {report.provider}  (from active SeasonRules)")
    add(f"  schedule provider {report.schedule_provider}  (decides the week)")
    add(f"  discovery window  {report.window_start}  ..  {report.window_end}")
    add("                    (a DISCOVERY filter only -- it never decides the week)")
    add("")
    add(f"  events observed   {report.events_observed}")
    add(f"  in requested week {len(report.eligible) + len(report.created) + len(report.reused)}")
    add(f"  refused (other/unknown week)  {len(report.refused)}")
    if report.applied:
        add(f"  games created     {len(report.created)}")
        add(f"  games reused      {len(report.reused)}")
    add(f"  conflicts         {len(report.conflicts)}")
    add(f"  unmapped teams    {len(report.unmapped_teams)}")
    add(f"  quota cost        {report.quota_cost}   (/events is free)")
    add(f"  quota remaining   {report.quota_remaining}")
    add("")

    shown = report.all_games if report.applied else report.eligible
    if shown:
        heading = "registered games" if report.applied else "would register (week verified)"
        add(f"--- {heading} ---------------------------------------------")
        for g in sorted(shown, key=lambda g: g.kickoff_at):
            flag = "NEW " if g.created else ("    " if g.previewed else "kept")
            add(f"  {flag} wk{g.week}  {g.away} @ {g.home}  {g.kickoff_at.isoformat()}")
            if g.game_id is not None:
                add(f"       game_id      {g.game_id}")
            add(f"       external_ref {g.external_ref}")
    else:
        add("--- no events in the requested week ---------------------------------")
        add("  Nothing matched. If this is unexpected, check the discovery window")
        add("  against the actual week -- the window does NOT define the week.")

    for label, items in (
        ("REFUSED (not the requested week)", report.refused),
        ("CONFLICTS", report.conflicts),
        ("UNMAPPED TEAMS", report.unmapped_teams),
        ("FAILURES", report.failures),
    ):
        if items:
            add("")
            add(f"--- {label} ---")
            for item in items:
                add(f"  {item}")
    add("")
    if report.applied:
        add("No quotes, no roster, no markets, no checkpoint rows were written.")
    else:
        add("PREVIEW: no Game rows were written. Re-run with --apply to persist.")
    add("=" * 78)
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register a week's provider events as Game rows (free, identity only)"
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--week-number", required=True, type=int)
    parser.add_argument(
        "--days-ahead", type=int, default=21,
        help="how far forward to DISCOVER events. This is a discovery filter "
             "only -- it never decides which NFL week an event belongs to. "
             "Default 21 days so a later week is reachable; anything outside "
             "the requested week is refused, not relabelled.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write Game rows. WITHOUT this flag the command is a "
             "preview that writes nothing.",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    report = register_week_events(
        season_id=args.season_id,
        week_number=args.week_number,
        window_start=now,
        window_end=now + timedelta(days=args.days_ahead),
        apply=args.apply,
    )
    print(render(report))
    return 1 if report.failures or report.conflicts else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
