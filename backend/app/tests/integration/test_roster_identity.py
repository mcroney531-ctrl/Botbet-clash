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
