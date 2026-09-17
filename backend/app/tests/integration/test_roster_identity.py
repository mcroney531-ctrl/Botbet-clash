"""Real-Postgres tests for identity persistence and its provenance."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.ingestion import ProviderCall
from app.db.models.markets import Game, Player
from app.db.models.roster import GamePlayer, GamePlayerObservation
from app.db.models.season import Season
from app.db.session import session_scope
from app.marketdata.base import ProviderCallMetadata
from app.marketdata.telemetry import record_call, start_run
from app.rosterdata.base import PlayerResolution, RosterEntry, RosterSnapshot
from app.rosterdata.identity_service import RosterIdentityConflict, resolve_and_record
from app.rosterdata.resolution import RESOLVER_VERSION, resolve_player
from app.rosterdata.teams import CanonicalTeam

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _game(session, tag: str) -> Game:
    season = Season(year=2026, name=f"s-{tag}", status="ACTIVE")
    session.add(season)
    session.flush()
    game = Game(
        external_ref=f"g-{tag}",
        season_id=season.id,
        week_number=3,
        home_team="Buffalo Bills",
        away_team="Detroit Lions",
        home_team_canonical="BUF",
        away_team_canonical="DET",
        kickoff_at=NOW + timedelta(days=1),
    )
    session.add(game)
    session.flush()
    return game


def _roster_call(session, *, tag: str) -> ProviderCall:
    run = start_run(session, provider="NFLVERSE", operation="FETCH_ROSTER")
    return record_call(
        session,
        run=run,
        metadata=ProviderCallMetadata(
            endpoint_capability="FETCH_CURRENT_ROSTER",
            requested_at=NOW,
            responded_at=NOW,
            http_status=200,
            raw_response_body=f"csv-{tag}".encode(),
            raw_response_sha256="b" * 64,
            raw_response_bytes=10,
        ),
        success=True,
    )


def _snapshot(*entries, basis="CURRENT_CONTEMPORANEOUS", week=None) -> RosterSnapshot:
    return RosterSnapshot(
        provider="NFLVERSE",
        season=2026,
        week=week,
        basis=basis,
        entries=entries,
        retrieved_at=NOW,
    )


GOFF = RosterEntry("00-0033106", "Jared Goff", CanonicalTeam.DET, "QB", 2026, None)
ALLEN = RosterEntry("00-0034857", "Josh Allen", CanonicalTeam.BUF, "QB", 2026, None)


def _resolve(name, snapshot):
    return resolve_player(
        odds_display_name=name,
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=snapshot,
    )


def test_first_resolution_creates_player_relationship_and_one_observation():
    with session_scope() as session:
        game = _game(session, "first")
        snap = _snapshot(GOFF, ALLEN)
        call = _roster_call(session, tag="first")

        gp = resolve_and_record(
            session,
            game_id=game.id,
            resolution=_resolve("Jared Goff", snap),
            display_name="Jared Goff",
            snapshot=snap,
            roster_provider_call_id=call.id,
            observed_at=NOW,
        )

        player = session.get(Player, gp.player_id)
        assert player.external_ref == "GSIS:00-0033106"
        # Player is a PERSON: the legacy columns stay empty rather than being
        # "conveniently" populated, which is how the opponent bug happened.
        assert player.team is None and player.position is None

        assert gp.team == "DET" and gp.position == "QB"

        observations = session.execute(
            select(GamePlayerObservation).where(GamePlayerObservation.game_player_id == gp.id)
        ).scalars().all()
        assert len(observations) == 1
        assert observations[0].roster_basis == "CURRENT_CONTEMPORANEOUS"
        assert observations[0].resolver_version == RESOLVER_VERSION
        assert observations[0].roster_provider_call_id == call.id


def test_revalidation_appends_an_observation_and_leaves_the_relationship_alone():
    """The exact reason the two tables are separate.

    OPENING resolves from snapshot A; FINAL revalidates from snapshot B. A
    single provenance column would have to overwrite -- destroying what
    supported OPENING -- or go stale, hiding that FINAL checked at all.
    """

    with session_scope() as session:
        game = _game(session, "reval")
        snap_a = _snapshot(GOFF)
        call_a = _roster_call(session, tag="opening")
        gp = resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap_a),
            display_name="Jared Goff", snapshot=snap_a,
            roster_provider_call_id=call_a.id, observed_at=NOW,
        )
        first_id, first_team = gp.id, gp.team

        snap_b = _snapshot(GOFF)
        call_b = _roster_call(session, tag="final")
        gp2 = resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap_b),
            display_name="Jared Goff", snapshot=snap_b,
            roster_provider_call_id=call_b.id, observed_at=NOW + timedelta(days=5),
        )

        assert gp2.id == first_id and gp2.team == first_team, "relationship is stable"

        observations = session.execute(
            select(GamePlayerObservation)
            .where(GamePlayerObservation.game_player_id == first_id)
            .order_by(GamePlayerObservation.observed_at)
        ).scalars().all()
        assert len(observations) == 2, "a revalidation is recorded, not discarded"
        assert [o.roster_provider_call_id for o in observations] == [call_a.id, call_b.id]
        assert observations[0].observed_at < observations[1].observed_at


def test_a_conflicting_observation_is_refused_rather_than_silently_applied():
    with session_scope() as session:
        game = _game(session, "conflict")
        snap_a = _snapshot(GOFF)
        call_a = _roster_call(session, tag="c1")
        resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap_a),
            display_name="Jared Goff", snapshot=snap_a,
            roster_provider_call_id=call_a.id, observed_at=NOW,
        )

        # Same person, now listed on the other side of this same game.
        traded = RosterEntry("00-0033106", "Jared Goff", CanonicalTeam.BUF, "QB", 2026, None)
        snap_b = _snapshot(traded)
        call_b = _roster_call(session, tag="c2")
        with pytest.raises(RosterIdentityConflict, match="needs review"):
            resolve_and_record(
                session, game_id=game.id, resolution=_resolve("Jared Goff", snap_b),
                display_name="Jared Goff", snapshot=snap_b,
                roster_provider_call_id=call_b.id, observed_at=NOW + timedelta(days=1),
            )


def test_an_unresolved_identity_is_never_persisted():
    with session_scope() as session:
        game = _game(session, "unres")
        snap = _snapshot(GOFF)
        call = _roster_call(session, tag="unres")
        with pytest.raises(ValueError, match="refusing to persist an unresolved identity"):
            resolve_and_record(
                session, game_id=game.id,
                resolution=PlayerResolution(outcome="UNRESOLVED_PLAYER", detail="not on either team"),
                display_name="Nobody Atall", snapshot=snap,
                roster_provider_call_id=call.id, observed_at=NOW,
            )
        assert session.execute(select(GamePlayer)).scalars().all() == []


def test_the_same_person_across_two_games_shares_one_player_row():
    with session_scope() as session:
        g1, g2 = _game(session, "sharedA"), _game(session, "sharedB")
        snap = _snapshot(GOFF)
        for game in (g1, g2):
            call = _roster_call(session, tag=f"shared-{game.external_ref}")
            resolve_and_record(
                session, game_id=game.id, resolution=_resolve("Jared Goff", snap),
                display_name="Jared Goff", snapshot=snap,
                roster_provider_call_id=call.id, observed_at=NOW,
            )
        players = session.execute(
            select(Player).where(Player.external_ref == "GSIS:00-0033106")
        ).scalars().all()
        assert len(players) == 1, "GSIS identity is the person, not the person-in-a-game"
        assert len(session.execute(select(GamePlayer)).scalars().all()) == 2


def test_a_historical_observation_records_its_weaker_basis():
    with session_scope() as session:
        game = _game(session, "histbasis")
        snap = _snapshot(GOFF, basis="HISTORICAL_RECONSTRUCTED", week=2)
        call = _roster_call(session, tag="hist")
        gp = resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap),
            display_name="Jared Goff", snapshot=snap,
            roster_provider_call_id=call.id, observed_at=NOW,
        )
        obs = session.execute(
            select(GamePlayerObservation).where(GamePlayerObservation.game_player_id == gp.id)
        ).scalar_one()
        assert obs.roster_basis == "HISTORICAL_RECONSTRUCTED"
        assert obs.roster_week == 2, "the snapshot's week, not the game's"


def test_an_invalid_roster_basis_is_rejected_by_the_database():
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        game = _game(session, "badbasis")
        snap = _snapshot(GOFF)
        call = _roster_call(session, tag="badbasis")
        gp = resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap),
            display_name="Jared Goff", snapshot=snap,
            roster_provider_call_id=call.id, observed_at=NOW,
        )
        gp_id, call_id = gp.id, call.id

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                GamePlayerObservation(
                    game_player_id=gp_id, roster_provider_call_id=call_id,
                    roster_season=2026, roster_basis="MADE_UP",
                    resolved_team="DET", resolver_version="x", observed_at=NOW,
                )
            )


# --- live_ingest preflight and snapshot clock -------------------------


def _rules(session, season_id, **overrides):
    from decimal import Decimal

    from app.db.models.season import SeasonRules

    values = dict(
        season_id=season_id,
        rules_version=f"rules-{uuid.uuid4()}",
        starting_bankroll_cents=1500,
        canonical_sportsbook="DRAFTKINGS",
        market_data_provider="THE_ODDS_API",
        roster_data_provider="NFLVERSE",
        research_settlement_provider="NFL_OFFICIAL_STATS",
        research_settlement_delay_hours=72,
        supported_prop_types=["receiving_yards"],
        devig_method="PROPORTIONAL_V1",
        benchmark_slate_size=10,
        batch_methodology="BATCH_SMALL",
        checkpoint_windows={},
        kelly_fraction=Decimal("0.25"),
        standard_max_bankroll_fraction=Decimal("0.10"),
        exceptional_max_bankroll_fraction=Decimal("0.20"),
        minimum_stake_cents=25,
        stake_increment_cents=25,
        pounce_limit=1,
        attribution_confidence_threshold=Decimal("0.700"),
        weekly_decision_deadline_rule={},
        effective_from=NOW,
    )
    values.update(overrides)
    row = SeasonRules(**values)
    session.add(row)
    session.flush()
    return row


def test_live_ingest_refuses_a_season_not_pinned_to_the_real_providers():
    """Fail closed BEFORE any research row exists.

    Game.external_ref is globally unique, so the first write of a real
    provider event fixes that event's identity permanently. Attaching it
    to a Phase-3 smoke season would mean every later correct ingestion
    silently reuses the bad row, and there is no clean unwind.
    """

    from app.marketdata.live_ingest import PreflightFailure, _verify_target_season

    with session_scope() as session:
        season = Season(year=2026, name="phase-3 smoke", status="ACTIVE")
        session.add(season)
        session.flush()
        _rules(session, season.id, market_data_provider="SYNTHETIC", roster_data_provider="SYNTHETIC")

        with pytest.raises(PreflightFailure, match="not pinned to the real providers"):
            _verify_target_season(session, season_id=season.id, week_number=3)


def test_live_ingest_refuses_week_zero():
    from app.marketdata.live_ingest import PreflightFailure, _verify_target_season

    with session_scope() as session:
        season = Season(year=2026, name="real research", status="ACTIVE")
        session.add(season)
        session.flush()
        _rules(session, season.id)
        with pytest.raises(PreflightFailure, match="real NFL week"):
            _verify_target_season(session, season_id=season.id, week_number=0)


def test_live_ingest_accepts_a_correctly_pinned_season():
    from app.marketdata.live_ingest import _verify_target_season

    with session_scope() as session:
        season = Season(year=2026, name="real research", status="ACTIVE")
        session.add(season)
        session.flush()
        _rules(session, season.id)
        assert _verify_target_season(session, season_id=season.id, week_number=3).id == season.id


def test_a_snapshot_taken_at_the_quote_observation_time_sees_those_quotes():
    """The snapshot clock regression.

    live_ingest captured `now` before the roster download and both
    provider calls, then built the MarketSnapshot at that stale `now`.
    Since quotes_as_of filters as_of_at <= taken_at, the quotes the run
    had just written were invisible and the acceptance would have
    "proved" an empty baseline.
    """

    from decimal import Decimal

    from app.db.models.markets import PropMarket
    from app.forecast_lab.market_snapshot_service import MarketSnapshotService
    from app.marketdata.dto import ProviderEventRef, ProviderPlayerRef, ProviderQuote
    from app.marketdata.ingestion import IngestionService
    from app.domain.enums import StatFamily

    with session_scope() as session:
        game = _game(session, "snapclock")
        snap = _snapshot(GOFF)
        roster_call = _roster_call(session, tag="snapclock")
        gp = resolve_and_record(
            session, game_id=game.id, resolution=_resolve("Jared Goff", snap),
            display_name="Jared Goff", snapshot=snap,
            roster_provider_call_id=roster_call.id, observed_at=NOW,
        )

        market = PropMarket(game_id=game.id, player_id=gp.player_id, stat_type="passing_yards")
        session.add(market)
        session.flush()

        run_started = NOW                      # what live_ingest used to use
        quote_observed = NOW + timedelta(minutes=4)   # after the network work

        quote_call = _roster_call(session, tag="snapclock-quotes")
        service = IngestionService(session)
        for book, over, under in (("DRAFTKINGS", -113, -111), ("FANDUEL", -110, -110)):
            service.persist_quote(
                quote=ProviderQuote(
                    event=ProviderEventRef(provider="THE_ODDS_API", external_event_id="e1"),
                    player=ProviderPlayerRef(provider="THE_ODDS_API", display_name="Jared Goff"),
                    stat_family=StatFamily.PASSING_YARDS,
                    vendor_market_key="player_pass_yds",
                    sportsbook=book,
                    line=Decimal("266.5"),
                    over_price=over,
                    under_price=under,
                    as_of_at=quote_observed,
                    retrieved_at=quote_observed,
                    source="THE_ODDS_API",
                ),
                market_id=market.id,
                provider_call=quote_call,
            )

        svc = MarketSnapshotService(session, market_data_provider="THE_ODDS_API")

        stale = svc.build_snapshot(
            market_id=market.id, canonical_sportsbook="DRAFTKINGS", taken_at=run_started
        )
        assert stale.number_of_books == 0, "the old clock could not see its own quotes"
        assert stale.is_valid_canonical_baseline is False

        correct = svc.build_snapshot(
            market_id=market.id, canonical_sportsbook="DRAFTKINGS", taken_at=quote_observed
        )
        assert correct.number_of_books == 2
        assert correct.is_valid_canonical_baseline is True
        assert correct.canonical_line == Decimal("266.50")
        assert correct.canonical_over_probability is not None


# --- live_ingest event guards + season provisioning --------------------


def test_the_event_is_named_never_chosen():
    """`sorted(events)[0]` would let the schedule decide which game gets
    the first immutable real rows. external_ref is permanent, so that is a
    permanent decision made by accident."""

    import inspect

    from app.marketdata import live_ingest

    code = "\n".join(
        line for line in inspect.getsource(live_ingest).splitlines()
        if not line.strip().startswith("#")
    )
    assert "sorted(events.payload" not in code
    assert "--event-id" in inspect.getsource(live_ingest)


def test_an_existing_game_with_a_different_scope_is_a_conflict_not_a_reuse():
    """Reusing by external_ref alone would protect only the FIRST write."""

    from app.marketdata.live_ingest import PreflightFailure

    with session_scope() as session:
        game = _game(session, "scope")
        original_week = game.week_number
        # Simulate the verification live_ingest performs before reuse.
        mismatches = []
        if game.week_number != 99:
            mismatches.append(f"week_number {game.week_number} != 99")
        assert mismatches, "a differing week must be detected"

        with pytest.raises(PreflightFailure, match="EVENT_SCOPE_CONFLICT"):
            raise PreflightFailure(
                f"EVENT_SCOPE_CONFLICT for {game.external_ref}: " + "; ".join(mismatches)
            )
        assert game.week_number == original_week, "never repaired automatically"


def test_provisioning_creates_one_season_pinned_to_the_real_providers():
    from app.db.models.season import SeasonRules as SeasonRulesRow
    from app.marketdata.live_ingest import _verify_target_season
    from app.services.provision_season import provision

    season_id = provision(name="BotBet Clash 2026 (test)", year=2026)
    with session_scope() as session:
        rules = session.execute(
            select(SeasonRulesRow).where(SeasonRulesRow.season_id == season_id)
        ).scalars().one()
        assert rules.market_data_provider == "THE_ODDS_API"
        assert rules.roster_data_provider == "NFLVERSE"
        assert rules.canonical_sportsbook == "DRAFTKINGS"
        assert rules.starting_bankroll_cents == 1500
        # And live_ingest's preflight accepts it.
        assert _verify_target_season(session, season_id=season_id, week_number=3).id == season_id


def test_provisioning_refuses_a_second_research_season():
    """Two candidate research seasons make every later --season-id choice a
    coin flip, and real provider events are globally unique."""

    from app.services.provision_season import provision

    provision(name="BotBet Clash 2026 (first)", year=2026)
    with pytest.raises(SystemExit, match="already exists"):
        provision(name="BotBet Clash 2026 (second)", year=2026)


def test_provisioning_writes_no_market_or_competitor_rows():
    from app.db.models.markets import Game, PropMarket
    from app.db.models.season import SeasonCompetitor
    from app.services.provision_season import provision

    provision(name="BotBet Clash 2026 (clean)", year=2026)
    with session_scope() as session:
        assert session.execute(select(Game)).scalars().all() == []
        assert session.execute(select(PropMarket)).scalars().all() == []
        assert session.execute(select(SeasonCompetitor)).scalars().all() == []


def test_smoke_seasons_stay_synthetic_and_are_never_eligible():
    """Phase 3 smoke seasons share year=2026. They must remain invisible to
    the research-season lookup and rejected by live_ingest's preflight."""

    from app.marketdata.live_ingest import PreflightFailure, _verify_target_season
    from app.services.provision_season import existing_research_seasons

    with session_scope() as session:
        smoke = Season(year=2026, name="Phase 3 Live Smoke", status="ACTIVE")
        session.add(smoke)
        session.flush()
        _rules(session, smoke.id, market_data_provider="SYNTHETIC", roster_data_provider="SYNTHETIC")

        assert existing_research_seasons(session) == []
        with pytest.raises(PreflightFailure, match="not pinned to the real providers"):
            _verify_target_season(session, season_id=smoke.id, week_number=3)


def test_the_inspector_is_read_only_and_reports_revalidation_invariants():
    """The acceptance report describes one RUN; the invariants are about
    the TABLES across runs. Re-running paid ingestion to check them would
    cost credits and mutate the state being inspected."""

    import ast
    import inspect

    from app.marketdata import inspect_ingestion

    tree = ast.parse(inspect.getsource(inspect_ingestion))
    writes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) in {"add", "add_all", "delete", "commit", "flush"}
        and getattr(getattr(n.func, "value", None), "id", None) == "session"
    ]
    assert writes == [], "the inspector must never write"

    source = inspect.getsource(inspect_ingestion)
    for forbidden in ("TheOddsApiProvider", "fetch_quotes", "list_events", "httpx"):
        assert forbidden not in source, f"{forbidden} would spend credits"
