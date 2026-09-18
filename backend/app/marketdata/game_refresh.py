"""The production market-data refresh for one already-identified game.

This is what an official checkpoint cycle calls before it captures. It is
NOT an acceptance script: it takes a `Game` that already exists and
refreshes its quotes, so there is no `--event-id` for an operator to get
wrong. `Game.external_ref` already holds the provider event identity, and
that identity is permanent.

What it does:

    nflverse current roster   -> identity resolution + GamePlayerObservation
    The Odds API event odds   -> PropMarket + immutable PropQuotes
                              -> COMMIT

What it deliberately does NOT do:

    no MarketSnapshot      that is the capture's job, at the capture clock
    no EvidenceSnapshot    same
    no model call          Phase 3 territory, and never inside ingestion
    no list_events call    the game identity is already persisted, so
                           re-discovering it would spend a credit to learn
                           something we already know permanently

The quote-persistence loop is shared with `live_ingest` rather than
copied. A second implementation of identity resolution would be able to
drift from the one that wrote the existing research record, which is the
same argument that made quote selection a single pure function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ingestion import IngestionRun, ProviderCall
from app.db.models.markets import Game
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.marketdata.dto import ProviderEventRef
from app.marketdata.ingestion import IngestionService, partition_ambiguous_lines
from app.marketdata.providers.the_odds_api import (
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
from app.rosterdata.teams import CanonicalTeam


@dataclass
class PersistCounts:
    """What one persistence pass actually wrote. Shared between the
    production refresh and the acceptance CLI so both report the same
    facts from the same code."""

    quotes_observed: int = 0
    quotes_written: int = 0
    quotes_deduplicated: int = 0
    markets_touched: int = 0
    resolved: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    books: list[str] = field(default_factory=list)
    provider_market_change_age_seconds: list[float] = field(default_factory=list)


def persist_resolved_quotes(
    session: Session,
    *,
    game: Game,
    home: CanonicalTeam,
    away: CanonicalTeam,
    quotes,
    roster_snapshot,
    roster_provider_call_id: uuid.UUID,
    provider_call: ProviderCall,
    run: IngestionRun | None,
    observed_at: datetime,
    counts: PersistCounts | None = None,
) -> PersistCounts:
    """Resolve identities and append immutable quotes. ONE implementation.

    A second copy of this loop could drift from the one that wrote the
    existing research record — different alias handling, a different
    quarantine rule — and nothing would flag it, because both would keep
    producing plausible rows.
    """

    counts = counts or PersistCounts()
    service = IngestionService(session)
    resolved_cache: dict[str, uuid.UUID | None] = {}

    for quote in quotes:
        name = quote.player.display_name
        if name not in resolved_cache:
            resolution = resolve_player(
                odds_display_name=name,
                home_team=home,
                away_team=away,
                snapshot=roster_snapshot,
                provider=ODDS_PROVIDER,
            )
            if not resolution.resolved:
                resolved_cache[name] = None
                bucket = (
                    counts.ambiguous
                    if resolution.outcome == "AMBIGUOUS_PLAYER"
                    else counts.unresolved
                )
                if name not in bucket:
                    bucket.append(name)
                continue
            try:
                game_player = resolve_and_record(
                    session,
                    game_id=game.id,
                    resolution=resolution,
                    display_name=name,
                    snapshot=roster_snapshot,
                    roster_provider_call_id=roster_provider_call_id,
                    observed_at=observed_at,
                )
            except RosterIdentityConflict as exc:
                resolved_cache[name] = None
                counts.conflicts.append(str(exc))
                continue
            resolved_cache[name] = game_player.player_id
            label = (
                f"{name} [{resolution.team.value}/{resolution.position}] "
                f"vs {resolution.opponent.value}"
            )
            if label not in counts.resolved:
                counts.resolved.append(label)

        player_id = resolved_cache[name]
        if player_id is None:
            continue

        market = service.resolve_prop_market(
            game_id=game.id, player_id=player_id, stat_type=quote.stat_family.value
        )
        counts.markets_touched += 1
        written = service.persist_quote(
            quote=quote, market_id=market.id, provider_call=provider_call, run=run
        )
        if written is None:
            counts.quotes_deduplicated += 1
        else:
            counts.quotes_written += 1
            if quote.provider_market_updated_at:
                counts.provider_market_change_age_seconds.append(
                    (quote.as_of_at - quote.provider_market_updated_at).total_seconds()
                )
        if quote.sportsbook not in counts.books:
            counts.books.append(quote.sportsbook)

    return counts


def event_ref_from_game(game: Game) -> ProviderEventRef:
    """`Game.external_ref` is "<PROVIDER>:<vendor event id>".

    Split on the FIRST colon only: a vendor id containing one would
    otherwise be silently truncated into a different event.
    """

    provider, _, external_event_id = game.external_ref.partition(":")
    if not provider or not external_event_id:
        raise ValueError(
            f"game {game.id} has external_ref {game.external_ref!r}, which is not "
            "a provider-scoped event reference"
        )
    return ProviderEventRef(provider=provider, external_event_id=external_event_id)


def refresh_game_market_data(
    *,
    game_id: uuid.UUID,
    market_data_provider: str,
    sport: str = DEFAULT_SPORT,
    odds_provider=None,
    roster_provider=None,
    now_fn=lambda: datetime.now(timezone.utc),
):
    """Refresh one game's market data and COMMIT. Returns a RefreshOutcome.

    Two provider calls, not three: the roster and the event odds. The
    game's identity is already persisted, so `list_events` would spend a
    credit to rediscover something permanent.
    """

    # Imported here rather than at module scope: checkpoint_cycle imports
    # the official runner's world, and a module-level cycle would make the
    # import graph depend on which side was loaded first.
    from app.marketdata.checkpoint_cycle import RefreshOutcome

    started_at = now_fn()
    provider_calls = 0
    quota_cost = 0

    def failure(category: str, message: str) -> "RefreshOutcome":
        return RefreshOutcome(
            ok=False,
            started_at=started_at,
            completed_at=now_fn(),
            error=sanitize_message(message),
            error_category=category,
            provider_calls=provider_calls,
            quota_cost=quota_cost,
        )

    with session_scope() as session:
        game = session.get(Game, game_id)
        if game is None:
            raise LookupError(f"game {game_id} not found")
        event = event_ref_from_game(game)
        home = CanonicalTeam(game.home_team_canonical)
        away = CanonicalTeam(game.away_team_canonical)
        season_year = _season_year(session, game)

    if event.provider != market_data_provider:
        raise ValueError(
            f"game {game_id} was written by {event.provider} but the season's "
            f"rules pin {market_data_provider}; refusing to blend two feeds"
        )

    odds = odds_provider or TheOddsApiProvider()
    roster = roster_provider or NflverseRosterProvider()

    # --- 1. roster (no credential, no credits) ------------------------
    roster_result = roster.fetch_current_roster(season=season_year)
    provider_calls += 1
    with session_scope() as session:
        roster_run = start_run(
            session, provider=ROSTER_PROVIDER, operation="FETCH_ROSTER", now=started_at
        )
        roster_call = record_call(
            session,
            run=roster_run,
            metadata=roster_result.call_metadata,
            success=roster_result.ok,
            error_category=roster_result.error.category if roster_result.error else None,
            error_message=roster_result.error.message if roster_result.error else None,
        )
        roster_call_id = roster_call.id
        finish_run(session, run=roster_run, status="SUCCEEDED" if roster_result.ok else "FAILED")

    if not roster_result.ok:
        return failure(roster_result.error.category, roster_result.error.message)

    roster_snapshot = roster_result.payload
    # Freshness checked at the point of USE, after the fetch: reading the
    # clock before the download makes the freshest possible roster look
    # NEGATIVELY aged.
    age_hours = roster_snapshot.age_hours(at=now_fn())
    if age_hours > LIVE_ROSTER_MAX_AGE_HOURS:
        return failure(
            "MALFORMED_RESPONSE",
            f"STALE_ROSTER_SNAPSHOT: {age_hours:.2f}h exceeds the "
            f"{LIVE_ROSTER_MAX_AGE_HOURS}h live tolerance",
        )

    # --- 2. quotes for the already-identified event -------------------
    quotes_result = odds.fetch_quotes(
        event=event, stat_families=list(StatFamily), sport=sport
    )
    provider_calls += 1
    quota_cost += quotes_result.call_metadata.quota_cost or 0

    with session_scope() as session:
        odds_run = start_run(
            session, provider=ODDS_PROVIDER, operation="FETCH_QUOTES",
            sport=sport, now=started_at,
        )
        quote_call = record_call(
            session,
            run=odds_run,
            metadata=quotes_result.call_metadata,
            success=quotes_result.ok,
            error_category=quotes_result.error.category if quotes_result.error else None,
            error_message=quotes_result.error.message if quotes_result.error else None,
            diagnostics=quotes_result.diagnostics,
        )
        odds_run_id, quote_call_id = odds_run.id, quote_call.id
        if not quotes_result.ok:
            finish_run(session, run=odds_run, status="FAILED")

    if not quotes_result.ok:
        return failure(quotes_result.error.category, quotes_result.error.message)

    accepted, alt_diagnostics = partition_ambiguous_lines(quotes_result.payload)

    # --- 3. persist and COMMIT ----------------------------------------
    with session_scope() as session:
        game = session.get(Game, game_id)
        counts = PersistCounts(quotes_observed=len(quotes_result.payload))
        counts.ambiguous.extend(
            f"{d.sportsbook}/{d.player_display_name}" for d in alt_diagnostics
        )
        persist_resolved_quotes(
            session,
            game=game,
            home=home,
            away=away,
            quotes=accepted,
            roster_snapshot=roster_snapshot,
            roster_provider_call_id=roster_call_id,
            provider_call=session.get(ProviderCall, quote_call_id),
            run=session.get(IngestionRun, odds_run_id),
            observed_at=now_fn(),
            counts=counts,
        )
        run_row = session.get(IngestionRun, odds_run_id)
        run_row.quotes_observed = counts.quotes_observed
        run_row.quotes_written = counts.quotes_written
        run_row.quotes_deduplicated = counts.quotes_deduplicated
        run_row.markets_quarantined = len(counts.ambiguous) + len(counts.unresolved)
        finish_run(
            session, run=run_row,
            status="SUCCEEDED" if counts.quotes_written else "PARTIAL",
        )

    return RefreshOutcome(
        ok=True,
        started_at=started_at,
        completed_at=now_fn(),
        quotes_written=counts.quotes_written,
        quotes_deduplicated=counts.quotes_deduplicated,
        provider_calls=provider_calls,
        quota_cost=quota_cost,
    )


def _season_year(session: Session, game: Game) -> int:
    from app.db.models.season import Season

    season = session.get(Season, game.season_id)
    if season is None:
        raise LookupError(f"game {game.id} references missing season {game.season_id}")
    return season.year
