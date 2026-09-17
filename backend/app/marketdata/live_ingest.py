"""Phase 4A.2 acceptance: one real event, end to end.

    python -m app.marketdata.live_ingest

Proves the full dual-provider chain on real data:

    The Odds API event      ->  canonical Game
    nflverse current roster ->  GSIS Player + GamePlayer + observation
    The Odds API quotes     ->  PropMarket + multi-book PropQuotes
                            ->  real MarketSnapshot

Deliberately SMALL. One event, one controlled call per stat family. It
does not sweep a week just to prove the path works -- that would spend
credits to demonstrate something a single game already demonstrates.

Runs from inside Railway (`railway ssh`). The dev sandbox cannot reach
the-odds-api.com; nflverse it can, which is why the roster half was
already validated before this existed.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select

from app.db.models.ingestion import IngestionRun, ProviderCall
from app.db.models.markets import Game, Player
from app.db.models.season import Season, SeasonRules
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.forecast_lab.market_snapshot_service import MarketSnapshotService
from app.marketdata.ingestion import IngestionService, partition_ambiguous_lines
from app.marketdata.providers.the_odds_api import (
    API_KEY_ENV_VAR,
    DEFAULT_SPORT,
    PROVIDER_NAME as ODDS_PROVIDER,
    TheOddsApiProvider,
)
from app.marketdata.telemetry import finish_run, record_call, sanitize_message, start_run
from app.rosterdata.base import LIVE_ROSTER_MAX_AGE_HOURS
from app.rosterdata.identity_service import RosterIdentityConflict, resolve_and_record
from app.rosterdata.providers.nflverse import PROVIDER_NAME as ROSTER_PROVIDER
from app.rosterdata.providers.nflverse import NflverseRosterProvider
from app.rosterdata.resolution import resolve_player
from app.rosterdata.teams import CanonicalTeam, TeamMappingError, canonical_from_odds_api

CANONICAL_BOOK = "DRAFTKINGS"

# A freshly fetched snapshot can read a few milliseconds "old" or "new"
# depending on where each clock was sampled. Anything beyond this is a real
# anomaly, not precision.
CLOCK_PRECISION_TOLERANCE_HOURS = 0.001  # 3.6 seconds


@dataclass
class Report:
    season: str | None = None
    event_label: str | None = None
    game_id: str | None = None
    home: str | None = None
    away: str | None = None
    roster_call_id: str | None = None
    roster_entries: int = 0
    roster_age_hours: float | None = None
    quotes_observed: int = 0
    quotes_written: int = 0
    quotes_deduplicated: int = 0
    markets_created: int = 0
    resolved: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    # TWO DIFFERENT AGES. Conflating them is a methodology error, so they
    # are never merged into one list or one label.
    #
    # observation_age = snapshot taken_at - quote.as_of_at
    #     How old OUR OBSERVATION is when a checkpoint consumes it. This is
    #     the candidate input to the eventual live freshness rule.
    #
    # provider_market_change_age = quote.as_of_at - provider_market_updated_at
    #     How long since the BOOK last moved this market. Diagnostic only;
    #     the seam forbids it from driving checkpoint eligibility, because a
    #     market left unchanged for an hour and successfully re-fetched one
    #     second ago is a FRESH observation of a quiet market.
    observation_age_seconds: list[float] = field(default_factory=list)
    provider_market_change_age_seconds: list[float] = field(default_factory=list)
    snapshot: dict | None = None
    books: list[str] = field(default_factory=list)
    # Phase 4A.3 freshness gate, as APPLIED by this run's snapshots. None
    # means no gate was configured, which is not the same as a gate that
    # excluded nothing -- the report says which it was.
    max_observation_age_seconds: int | None = None
    snapshots_built: int = 0
    snapshots_valid_baseline: int = 0
    snapshots_canonical_stale: int = 0
    stale_books_excluded: int = 0
    books_observed: int = 0
    quota_remaining: int | None = None
    quota_cost: int = 0
    failures: list[str] = field(default_factory=list)


class PreflightFailure(SystemExit):
    """The run refused to write. Raised before any research row exists."""


def _verify_target_season(session, *, season_id: uuid.UUID, week_number: int) -> Season:
    """Fail closed before a single research row is written.

    `Game.external_ref` is globally UNIQUE, so the first write of a real
    provider event fixes that event's identity forever. Attaching it to
    the wrong season -- or to week 0 -- would mean every later, correct
    ingestion silently finds and reuses the bad row. There is no clean
    unwind, which is why this is checked rather than defaulted.

    Selecting by year is specifically NOT allowed: `Season.year` is not
    unique and Phase 3's live smoke deliberately creates throwaway 2026
    seasons, so `.first()` could hand back a smoke season.
    """

    if week_number < 1:
        raise PreflightFailure(
            f"--week-number must be a real NFL week (got {week_number}). Week 0 "
            "would permanently mis-scope this event's globally unique external_ref."
        )

    season = session.get(Season, season_id)
    if season is None:
        raise PreflightFailure(f"no season {season_id}")

    rules = session.execute(
        select(SeasonRules)
        .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
        .order_by(SeasonRules.effective_from.desc())
    ).scalars().first()
    if rules is None:
        raise PreflightFailure(f"season {season_id} has no active SeasonRules")

    expected = {
        "market_data_provider": ODDS_PROVIDER,
        "roster_data_provider": ROSTER_PROVIDER,
        "canonical_sportsbook": CANONICAL_BOOK,
    }
    wrong = {
        field: (getattr(rules, field), want)
        for field, want in expected.items()
        if getattr(rules, field) != want
    }
    if wrong:
        detail = "; ".join(f"{f}={got!r} (expected {want!r})" for f, (got, want) in wrong.items())
        raise PreflightFailure(
            f"season {season_id} ({season.name}) is not pinned to the real providers: "
            f"{detail}.\nRefusing to write real research rows into a season whose "
            "rules point somewhere else. Create an intentional research season with "
            "these rules, or pass the right --season-id."
        )
    return season


def _require_key() -> None:
    if not os.environ.get(API_KEY_ENV_VAR):
        raise SystemExit(
            f"{API_KEY_ENV_VAR} is not set. Set it in the Railway service Variables "
            "UI and run this inside the container via `railway ssh`. Never pass it "
            "on a command line."
        )


def run(
    *,
    season_id: uuid.UUID,
    week_number: int,
    event_id: str,
    sport: str = DEFAULT_SPORT,
    max_observation_age_seconds: int | None = None,
) -> Report:
    report = Report()
    report.max_observation_age_seconds = max_observation_age_seconds
    _require_key()
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        season = _verify_target_season(session, season_id=season_id, week_number=week_number)
        season_year = season.year
        report.season = f"{season.name} ({season.year}) week {week_number}"

    odds = TheOddsApiProvider()
    roster_provider = NflverseRosterProvider()

    # --- 1. roster snapshot (no credits, no credential) ---------------
    roster_result = roster_provider.fetch_current_roster(season=season_year)

    with session_scope() as session:
        roster_run = start_run(session, provider=ROSTER_PROVIDER, operation="FETCH_ROSTER", now=now)
        roster_call = record_call(
            session,
            run=roster_run,
            metadata=roster_result.call_metadata,
            success=roster_result.ok,
            error_category=roster_result.error.category if roster_result.error else None,
            error_message=roster_result.error.message if roster_result.error else None,
        )
        report.roster_call_id = str(roster_call.id)
        roster_call_id = roster_call.id
        finish_run(session, run=roster_run, status="SUCCEEDED" if roster_result.ok else "FAILED")

    if not roster_result.ok:
        report.failures.append(f"roster: {roster_result.error.category}")
        return report
    snapshot = roster_result.payload
    report.roster_entries = len(snapshot.entries)

    # Freshness is evaluated at the point of USE, after the fetch. `now` was
    # read before the download while snapshot.retrieved_at is post-response,
    # so age_hours(at=now) is (earlier - later) -- negative, and most
    # negative for the freshest possible roster. That would have made the
    # acceptance report's freshness evidence meaningless on the very run
    # meant to demonstrate the 36-hour policy.
    roster_checked_at = datetime.now(timezone.utc)
    roster_age = snapshot.age_hours(at=roster_checked_at)
    if roster_age < -CLOCK_PRECISION_TOLERANCE_HOURS:
        report.failures.append(
            f"roster snapshot reports a negative age ({roster_age:.4f}h) when "
            "checked after the fetch, which should be impossible; refusing to "
            "treat an unexplained clock state as fresh"
        )
        return report
    report.roster_age_hours = round(max(roster_age, 0.0), 4)

    if report.roster_age_hours > LIVE_ROSTER_MAX_AGE_HOURS:
        report.failures.append(
            f"STALE_ROSTER_SNAPSHOT: {report.roster_age_hours}h exceeds the "
            f"{LIVE_ROSTER_MAX_AGE_HOURS}h live tolerance; blocking rather than "
            "silently accepting older roster context"
        )
        return report

    # --- 2. pick one real upcoming event -----------------------------
    events = odds.list_events(sport=sport, window_start=now, window_end=now + timedelta(days=8))
    with session_scope() as session:
        odds_run = start_run(session, provider=ODDS_PROVIDER, operation="FETCH_QUOTES",
                             sport=sport, now=now)
        odds_run_id = odds_run.id
        call = record_call(session, run=odds_run, metadata=events.call_metadata,
                           success=events.ok,
                           error_category=events.error.category if events.error else None,
                           error_message=events.error.message if events.error else None)
        report.quota_cost += events.call_metadata.quota_cost or 0
        if events.call_metadata.quota_remaining is not None:
            report.quota_remaining = events.call_metadata.quota_remaining

    if not events.ok or not events.payload:
        report.failures.append("no upcoming events available")
        with session_scope() as session:
            finish_run(session, run=session.get(IngestionRun, odds_run_id), status="FAILED")
        return report

    # The event is NAMED, never chosen. Picking sorted(events)[0] would let
    # the schedule decide which game receives the first immutable real rows,
    # and Game.external_ref is globally unique -- so an implicitly chosen
    # game is a permanent decision made by accident.
    matches = [e for e in events.payload if e.ref.external_event_id == event_id]
    if len(matches) != 1:
        report.failures.append(
            f"EVENT_NOT_FOUND: --event-id {event_id} matched {len(matches)} of "
            f"{len(events.payload)} returned events. Refusing to substitute a "
            "different game."
        )
        with session_scope() as session:
            finish_run(session, run=session.get(IngestionRun, odds_run_id), status="FAILED")
        return report
    event = matches[0]
    report.event_label = f"{event.away_team} @ {event.home_team}"

    try:
        home = canonical_from_odds_api(event.home_team)
        away = canonical_from_odds_api(event.away_team)
    except TeamMappingError as exc:
        report.failures.append(f"TEAM_MAPPING_FAILED: {exc}")
        with session_scope() as session:
            finish_run(session, run=session.get(IngestionRun, odds_run_id), status="FAILED")
        return report
    report.home, report.away = home.value, away.value

    # --- 3. quotes ----------------------------------------------------
    quotes_result = odds.fetch_quotes(event=event.ref, stat_families=list(StatFamily), sport=sport)
    with session_scope() as session:
        odds_run = session.get(IngestionRun, odds_run_id)
        quote_call = record_call(
            session, run=odds_run, metadata=quotes_result.call_metadata,
            success=quotes_result.ok,
            error_category=quotes_result.error.category if quotes_result.error else None,
            error_message=quotes_result.error.message if quotes_result.error else None,
            diagnostics=quotes_result.diagnostics,
        )
        quote_call_id = quote_call.id
        report.quota_cost += quotes_result.call_metadata.quota_cost or 0
        if quotes_result.call_metadata.quota_remaining is not None:
            report.quota_remaining = quotes_result.call_metadata.quota_remaining

    if not quotes_result.ok:
        report.failures.append(
            f"quotes: {quotes_result.error.category}: "
            f"{sanitize_message(quotes_result.error.message)}"
        )
        with session_scope() as session:
            finish_run(session, run=session.get(IngestionRun, odds_run_id), status="FAILED")
        return report

    accepted, alt_diagnostics = partition_ambiguous_lines(quotes_result.payload)
    # The snapshot MUST be taken at (or after) the quotes' own observation
    # time. `now` was captured before the roster download and both provider
    # calls, so building the snapshot at `now` would make quotes_as_of --
    # which filters as_of_at <= taken_at -- exclude the very rows this run
    # just wrote, and the acceptance would "prove" an empty baseline.
    snapshot_taken_at = max((q.as_of_at for q in accepted), default=now)
    report.quotes_observed = len(quotes_result.payload)
    report.ambiguous = [f"{d.sportsbook}/{d.player_display_name}" for d in alt_diagnostics]

    # --- 4. persist ---------------------------------------------------
    with session_scope() as session:
        repo = MarketRepository(session)
        game = session.execute(
            select(Game).where(Game.external_ref == event.ref.as_external_ref())
        ).scalar_one_or_none()
        if game is not None:
            # Reusing by external_ref alone would make the poisoned-event
            # protection cover only the FIRST write. Verify the whole scope.
            mismatches = []
            if game.season_id != season_id:
                mismatches.append(f"season_id {game.season_id} != {season_id}")
            if game.week_number != week_number:
                mismatches.append(f"week_number {game.week_number} != {week_number}")
            if game.home_team_canonical != home.value:
                mismatches.append(f"home {game.home_team_canonical} != {home.value}")
            if game.away_team_canonical != away.value:
                mismatches.append(f"away {game.away_team_canonical} != {away.value}")
            if game.kickoff_at != event.kickoff_at:
                mismatches.append(
                    f"kickoff {game.kickoff_at.isoformat()} != {event.kickoff_at.isoformat()}"
                )
            if mismatches:
                raise PreflightFailure(
                    f"EVENT_SCOPE_CONFLICT for {event.ref.as_external_ref()}: "
                    + "; ".join(mismatches)
                    + ".\nThe existing row is NOT being repaired or moved "
                    "automatically -- an external_ref is a permanent identity and "
                    "silently relocating it would hide whichever write was wrong."
                )
        if game is None:
            game = repo.create_game(
                external_ref=event.ref.as_external_ref(),
                season_id=season_id,
                week_number=week_number,
                home_team=event.home_team,
                away_team=event.away_team,
                home_team_canonical=home.value,
                away_team_canonical=away.value,
                kickoff_at=event.kickoff_at,
            )
        report.game_id = str(game.id)

        service = IngestionService(session)
        run_row = session.get(IngestionRun, odds_run_id)
        provider_call = session.get(ProviderCall, quote_call_id)

        resolved_cache: dict[str, uuid.UUID | None] = {}
        for quote in accepted:
            name = quote.player.display_name
            if name not in resolved_cache:
                resolution = resolve_player(
                    odds_display_name=name,
                    home_team=home,
                    away_team=away,
                    snapshot=snapshot,
                    provider=ODDS_PROVIDER,
                )
                if not resolution.resolved:
                    resolved_cache[name] = None
                    bucket = report.ambiguous if resolution.outcome == "AMBIGUOUS_PLAYER" else report.unresolved
                    if name not in bucket:
                        bucket.append(name)
                    continue
                try:
                    game_player = resolve_and_record(
                        session, game_id=game.id, resolution=resolution, display_name=name,
                        snapshot=snapshot, roster_provider_call_id=roster_call_id, observed_at=now,
                    )
                except RosterIdentityConflict as exc:
                    resolved_cache[name] = None
                    report.conflicts.append(str(exc))
                    continue
                resolved_cache[name] = game_player.player_id
                label = f"{name} [{resolution.team.value}/{resolution.position}] vs {resolution.opponent.value}"
                if label not in report.resolved:
                    report.resolved.append(label)

            player_id = resolved_cache[name]
            if player_id is None:
                continue

            market = service.resolve_prop_market(
                game_id=game.id, player_id=player_id, stat_type=quote.stat_family.value
            )
            report.markets_created += 1
            written = service.persist_quote(
                quote=quote, market_id=market.id, provider_call=provider_call, run=run_row
            )
            if written is None:
                report.quotes_deduplicated += 1
            else:
                report.quotes_written += 1
                if quote.provider_market_updated_at:
                    report.provider_market_change_age_seconds.append(
                        (quote.as_of_at - quote.provider_market_updated_at).total_seconds()
                    )
            if quote.sportsbook not in report.books:
                report.books.append(quote.sportsbook)

        run_row.quotes_observed = report.quotes_observed
        run_row.quotes_written = report.quotes_written
        run_row.quotes_deduplicated = report.quotes_deduplicated
        run_row.markets_quarantined = len(report.ambiguous) + len(report.unresolved)
        finish_run(
            session, run=run_row,
            status="SUCCEEDED" if report.quotes_written and not report.failures else "PARTIAL",
        )
        game_id = game.id

    # --- 5. one real MarketSnapshot ----------------------------------
    report.observation_age_seconds = [
        (snapshot_taken_at - q.as_of_at).total_seconds() for q in accepted
    ]

    with session_scope() as session:
        repo = MarketRepository(session)
        markets = repo.markets_for_game(game_id)
        service = MarketSnapshotService(
            session,
            market_data_provider=ODDS_PROVIDER,
            max_observation_age_seconds=max_observation_age_seconds,
        )
        for market in markets:
            snap = service.build_snapshot(
                market_id=market.id,
                canonical_sportsbook=CANONICAL_BOOK,
                taken_at=snapshot_taken_at,
            )
            # Counted over EVERY snapshot, not just the one displayed
            # below. A gate strict enough to invalidate every baseline
            # would otherwise leave the report saying nothing at all,
            # which is the one case where it most needs to speak.
            report.snapshots_built += 1
            report.snapshots_valid_baseline += int(snap.is_valid_canonical_baseline)
            report.snapshots_canonical_stale += int(snap.canonical_quote_stale)
            report.stale_books_excluded += snap.stale_books_excluded
            report.books_observed += snap.books_observed
            if snap.is_valid_canonical_baseline and report.snapshot is None:
                player = session.get(Player, market.player_id)
                report.snapshot = {
                    "player": player.name,
                    "player_external_ref": player.external_ref,
                    "stat_type": market.stat_type,
                    "canonical_book": snap.canonical_sportsbook,
                    "canonical_line": str(snap.canonical_line),
                    "canonical_over_price": snap.canonical_over_price,
                    "canonical_under_price": snap.canonical_under_price,
                    "canonical_over_probability": str(snap.canonical_over_probability),
                    "same_line_consensus_over_probability": str(
                        snap.same_line_consensus_over_probability
                    ),
                    "median_line": str(snap.market_median_line),
                    "min_line": str(snap.market_min_line),
                    "max_line": str(snap.market_max_line),
                    "number_of_books": snap.number_of_books,
                    "stale_books_excluded": snap.stale_books_excluded,
                }
    return report


def render(report: Report) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 72)
    add("PHASE 4A.2 — REAL CURRENT INGESTION")
    add("=" * 72)
    add(f"season:       {report.season}")
    add(f"event:        {report.event_label}  ({report.away} @ {report.home})")
    add(f"game_id:      {report.game_id}")
    add("")
    add("--- roster (nflverse, no credential, no credits) --------------------")
    add(f"  provider_call:   {report.roster_call_id}")
    add(f"  entries:         {report.roster_entries}")
    add(f"  snapshot age:    {report.roster_age_hours}h (tolerance {LIVE_ROSTER_MAX_AGE_HOURS}h)")
    add("")
    add("--- identity resolution --------------------------------------------")
    for label in report.resolved[:12]:
        add(f"  RESOLVED   {label}")
    if len(report.resolved) > 12:
        add(f"  ... and {len(report.resolved) - 12} more")
    add(f"  resolved:    {len(report.resolved)}")
    add(f"  UNRESOLVED:  {len(report.unresolved)} {report.unresolved[:6]}")
    add(f"  AMBIGUOUS:   {len(report.ambiguous)} {report.ambiguous[:6]}")
    add(f"  CONFLICTS:   {len(report.conflicts)}")
    add("")
    add("--- market persistence ---------------------------------------------")
    add(f"  quotes observed:     {report.quotes_observed}")
    add(f"  quotes written:      {report.quotes_written}")
    add(f"  quotes deduplicated: {report.quotes_deduplicated}")
    add(f"  books:               {', '.join(report.books)}")
    add("")
    add("--- (A) OBSERVATION AGE — candidate freshness metric ----------------")
    add("  snapshot taken_at - quote.as_of_at, in seconds.")
    add("  How old OUR OBSERVATION was when the snapshot consumed it. THIS is")
    add("  the age a live checkpoint freshness rule should be built on.")
    if report.observation_age_seconds:
        ages = sorted(report.observation_age_seconds)
        add(f"    min {ages[0]:.1f} | median {ages[len(ages)//2]:.1f} | max {ages[-1]:.1f}")
    else:
        add("    (none)")
    add("")
    add("--- (B) PROVIDER MARKET-CHANGE AGE — DIAGNOSTIC ONLY ---------------")
    add("  quote.as_of_at - provider_market_updated_at, in seconds.")
    add("  How long since the BOOK last moved this market. This is NOT quote")
    add("  freshness and MUST NOT drive checkpoint eligibility: a market left")
    add("  unchanged for an hour and successfully re-fetched one second ago is")
    add("  a fresh observation of a quiet market, not a stale quote.")
    if report.provider_market_change_age_seconds:
        ages = sorted(report.provider_market_change_age_seconds)
        add(f"    min {ages[0]:.0f} | median {ages[len(ages)//2]:.0f} | max {ages[-1]:.0f}")
    else:
        add("    (none)")
    add("")
    add("--- freshness gate (Phase 4A.3) ------------------------------------")
    gate = report.max_observation_age_seconds
    add(f"  max_observation_age_seconds: {gate if gate is not None else 'None (no gate applied)'}")
    add(f"  snapshots built:             {report.snapshots_built}")
    add(f"  books observed (total):      {report.books_observed}")
    add(f"  valid canonical baseline:    {report.snapshots_valid_baseline}")
    add(f"  canonical quote STALE:       {report.snapshots_canonical_stale}")
    add(f"  stale book-quotes excluded:  {report.stale_books_excluded}")
    add("")
    add("--- real MarketSnapshot --------------------------------------------")
    if report.snapshot:
        for k, v in report.snapshot.items():
            add(f"  {k:36} {v}")
    else:
        add("  none with a valid canonical baseline")
    add("")
    add(f"quota remaining: {report.quota_remaining} | cost this run: {report.quota_cost}")
    if report.failures:
        add("")
        add("--- FAILURES -------------------------------------------------------")
        for f in report.failures:
            add(f"  {f}")
    add("")
    add("=" * 72)
    add("PHASE 4A.2 CURRENT INGESTION: COMPLETE — REVIEW REQUIRED")
    add("=" * 72)
    return "\n".join(out)


def _non_negative_seconds(raw: str) -> int:
    """argparse type for the freshness tolerance.

    Rejected at PARSE time rather than deep inside snapshot construction:
    a negative tolerance marks every observation stale, so the run would
    complete "successfully" having reported a total market outage that
    never happened. The service validates it again -- this is the layer
    that keeps a typo from ever reaching it.
    """

    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer number of seconds") from None
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"must be >= 0, got {value}. A negative tolerance marks every "
            "observation stale, which reads as a total market outage rather "
            "than as the misconfiguration it is."
        )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4A.2 real current ingestion")
    parser.add_argument(
        "--season-id", required=True, type=uuid.UUID,
        help="the intentional research season. Never selected by year: Season.year "
             "is not unique and smoke seasons share it.",
    )
    parser.add_argument(
        "--week-number", required=True, type=int,
        help="the real NFL week. Week 0 is refused -- external_ref is global and permanent.",
    )
    parser.add_argument(
        "--event-id", required=True,
        help="the exact provider event id. The event is named, never chosen: "
             "external_ref is permanent, so an implicitly selected game is a "
             "permanent decision made by accident.",
    )
    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument(
        "--max-observation-age-seconds", type=_non_negative_seconds, default=None,
        help="per-book observation freshness gate for the snapshots this run "
             "builds. Omitted means NO gate -- which is the honest default until "
             "real captures say what live staleness looks like. Never derived "
             "from provider_market_updated_at.",
    )
    args = parser.parse_args(argv)

    report = run(
        season_id=args.season_id,
        week_number=args.week_number,
        event_id=args.event_id,
        sport=args.sport,
        max_observation_age_seconds=args.max_observation_age_seconds,
    )
    print(render(report))
    return 1 if report.failures or not report.quotes_written else 0


if __name__ == "__main__":
    sys.exit(main())
