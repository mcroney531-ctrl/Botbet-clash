"""Real-Postgres tests for ingestion persistence semantics.

These are integration tests deliberately: the things under test here are
UNIQUE constraints, NOT NULL provenance, ordering guarantees and a
migration backfill. SQLite would not reproduce any of them faithfully.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.ingestion import IngestionRun, ProviderCall
from app.db.models.markets import PropMarket, PropQuote
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.forecast_lab.market_snapshot_service import MarketSnapshotService
from app.marketdata.dto import ProviderEventRef, ProviderPlayerRef, ProviderQuote
from app.marketdata.ingestion import IngestionService
from app.marketdata.provenance import SYNTHETIC_CALL_ID, SYNTHETIC_SOURCE
from app.marketdata.telemetry import (
    finish_run,
    latest_quota_remaining,
    record_call,
    reserve_would_be_breached,
    start_run,
)
from app.marketdata.base import ProviderCallMetadata

PROVIDER = "TEST_PROVIDER"


def _fixture(session, tag: str):
    """A season/game/player/market skeleton to hang quotes on."""

    from app.db.models.markets import Game, Player
    from app.db.models.season import Season

    season = Season(year=2026, name=f"s-{tag}", status="ACTIVE")
    session.add(season)
    session.flush()
    game = Game(
        external_ref=f"g-{tag}",
        season_id=season.id,
        week_number=1,
        home_team="KC",
        away_team="SF",
        kickoff_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    player = Player(external_ref=f"p-{tag}", name="Player", team="KC", position="WR")
    session.add_all([game, player])
    session.flush()
    market = PropMarket(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
    session.add(market)
    session.flush()
    return season, game, player, market


def _call(session, *, provider=PROVIDER, cost=5, remaining=9000) -> ProviderCall:
    run = start_run(session, provider=provider, operation="FETCH_QUOTES")
    meta = ProviderCallMetadata(
        endpoint_capability="FETCH_QUOTES",
        requested_at=datetime.now(timezone.utc),
        responded_at=datetime.now(timezone.utc),
        http_status=200,
        quota_used=100,
        quota_remaining=remaining,
        quota_cost=cost,
        raw_response_body=b'{"ok":true}',
        raw_response_sha256="a" * 64,
        raw_response_bytes=11,
    )
    return record_call(session, run=run, metadata=meta, success=True)


def _quote(*, line="74.5", over=-115, under=-105, as_of, retrieved=None, book="DRAFTKINGS"):
    return ProviderQuote(
        event=ProviderEventRef(provider=PROVIDER, external_event_id="evt-1"),
        player=ProviderPlayerRef(
            provider=PROVIDER, display_name="Player", external_player_id="p1"
        ),
        stat_family=StatFamily.RECEIVING_YARDS,
        vendor_market_key="player_reception_yds",
        sportsbook=book,
        line=Decimal(line),
        over_price=over,
        under_price=under,
        as_of_at=as_of,
        retrieved_at=retrieved or as_of,
        source=PROVIDER,
    )


# --- 3/4. Replay idempotency vs. a genuinely new observation -----------


def test_replaying_the_same_provider_call_writes_nothing_twice():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "replay")
        call = _call(session)
        service = IngestionService(session)
        now = datetime.now(timezone.utc)
        quote = _quote(as_of=now)

        first = service.persist_quote(quote=quote, market_id=market.id, provider_call=call)
        second = service.persist_quote(quote=quote, market_id=market.id, provider_call=call)

        assert first is not None
        assert second is None, "reprocessing a stored response must be idempotent"
        rows = session.execute(
            select(PropQuote).where(PropQuote.market_id == market.id)
        ).scalars().all()
        assert len(rows) == 1


def test_a_new_poll_records_a_new_observation_even_when_nothing_moved():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "unchanged")
        service = IngestionService(session)
        t0 = datetime.now(timezone.utc) - timedelta(hours=4)
        t1 = datetime.now(timezone.utc)

        call_a = _call(session)
        service.persist_quote(quote=_quote(as_of=t0), market_id=market.id, provider_call=call_a)
        call_b = _call(session)
        service.persist_quote(quote=_quote(as_of=t1), market_id=market.id, provider_call=call_b)

        rows = session.execute(
            select(PropQuote).where(PropQuote.market_id == market.id).order_by(PropQuote.as_of_at)
        ).scalars().all()
        assert len(rows) == 2, (
            "identical prices four hours apart are two observations — retaining "
            "both is what distinguishes 'still quoted' from 'book disappeared'"
        )
        assert rows[0].line == rows[1].line


# --- 7/8/9. Quote selection: as_of_at, source pinning, tie ordering ----


def test_quote_selection_uses_observation_time_not_retrieval_time():
    """The backfill scenario the whole time model exists for."""

    with session_scope() as session:
        _, _, _, market = _fixture(session, "asof")
        service = IngestionService(session)

        sept_05 = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
        sept_20 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)

        # A backfill: retrieved on the 20th, representing the 5th.
        call = _call(session)
        service.persist_quote(
            quote=_quote(line="61.5", as_of=sept_05, retrieved=sept_20),
            market_id=market.id,
            provider_call=call,
        )

        repo = MarketRepository(session)
        at_sept_06 = repo.quotes_as_of(
            market.id, source=PROVIDER, as_of=sept_05 + timedelta(days=1)
        )
        assert len(at_sept_06) == 1, (
            "a 5 September market state must be visible to a 6 September "
            "checkpoint even though we fetched it on the 20th"
        )
        at_sept_04 = repo.quotes_as_of(
            market.id, source=PROVIDER, as_of=sept_05 - timedelta(days=1)
        )
        assert at_sept_04 == [], "and invisible before it existed"


def test_quote_selection_is_pinned_to_one_source():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "pin")
        service = IngestionService(session)
        now = datetime.now(timezone.utc)

        call = _call(session)
        service.persist_quote(quote=_quote(as_of=now), market_id=market.id, provider_call=call)
        # A different vendor quoting the same market.
        other = _quote(as_of=now, line="80.5")
        other = ProviderQuote(**{**other.__dict__, "source": "OTHER_VENDOR"})
        service.persist_quote(quote=other, market_id=market.id, provider_call=call)

        repo = MarketRepository(session)
        ours = repo.quotes_as_of(market.id, source=PROVIDER, as_of=now)
        theirs = repo.quotes_as_of(market.id, source="OTHER_VENDOR", as_of=now)

        assert len(ours) == 1 and ours[0].line == Decimal("74.50")
        assert len(theirs) == 1 and theirs[0].line == Decimal("80.50")


def test_tie_ordering_is_stable_across_repeated_queries():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "tie")
        service = IngestionService(session)
        same_instant = datetime.now(timezone.utc)

        for line in ("70.5", "71.5", "72.5"):
            service.persist_quote(
                quote=_quote(line=line, as_of=same_instant, book="FANDUEL"),
                market_id=market.id,
                provider_call=_call(session),
            )

        repo = MarketRepository(session)
        runs = [
            [q.id for q in repo.quotes_as_of(market.id, source=PROVIDER, as_of=same_instant)]
            for _ in range(5)
        ]
        assert all(r == runs[0] for r in runs), (
            "MarketSnapshotService takes the first row per book, so an unstable "
            "sort would make the canonical baseline non-reproducible"
        )


# --- 10. PropMarket natural key ---------------------------------------


def test_prop_market_natural_key_is_enforced_by_the_database():
    """Defence in depth: the constraint holds even if application-level
    get-or-create is bypassed entirely."""

    with session_scope() as session:
        _, game, player, _ = _fixture(session, "natkey")
        game_id, player_id = game.id, player.id

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                PropMarket(game_id=game_id, player_id=player_id, stat_type="receiving_yards")
            )


def test_resolving_the_same_market_twice_returns_one_row():
    with session_scope() as session:
        _, game, player, market = _fixture(session, "getorcreate")
        service = IngestionService(session)
        a = service.resolve_prop_market(
            game_id=game.id, player_id=player.id, stat_type="receiving_yards"
        )
        b = service.resolve_prop_market(
            game_id=game.id, player_id=player.id, stat_type="receiving_yards"
        )
        assert a.id == b.id == market.id


# --- 12. Placeholder roster values are refused -------------------------


@pytest.mark.parametrize("bad", ["UNKNOWN", "unknown", "N/A", "", "  ", "TBD"])
def test_placeholder_roster_values_are_prohibited(bad):
    with session_scope() as session:
        service = IngestionService(session)
        with pytest.raises(ValueError, match="placeholder roster"):
            service.resolve_player(
                external_ref="x:name:someone", name="Someone", team=bad, position="WR"
            )
        with pytest.raises(ValueError, match="placeholder roster"):
            service.resolve_player(
                external_ref="x:name:someone", name="Someone", team="KC", position=bad
            )


# --- 15. Missing canonical book is never substituted -------------------


def test_missing_draftkings_does_not_promote_another_book_to_canonical():
    with session_scope() as session:
        season, game, player, market = _fixture(session, "nodk")
        service = IngestionService(session)
        now = datetime.now(timezone.utc)
        call = _call(session)
        for book, line in (("FANDUEL", "74.5"), ("BETMGM", "75.5")):
            service.persist_quote(
                quote=_quote(book=book, line=line, as_of=now),
                market_id=market.id,
                provider_call=call,
            )

        snapshot = MarketSnapshotService(
            session, market_data_provider=PROVIDER
        ).build_snapshot(
            market_id=market.id, canonical_sportsbook="DRAFTKINGS", taken_at=now
        )

        assert snapshot.is_valid_canonical_baseline is False
        assert snapshot.canonical_line is None
        assert snapshot.canonical_over_probability is None
        assert snapshot.number_of_books == 2, "the other books still count for context"


# --- 17/18. Raw response + quota telemetry ----------------------------


def test_raw_response_hash_and_quota_are_persisted_per_call():
    with session_scope() as session:
        call = _call(session, cost=7, remaining=8123)
        stored = session.get(ProviderCall, call.id)
        assert stored.raw_response_body == b'{"ok":true}'
        assert stored.raw_response_bytes == 11
        assert stored.raw_response_sha256 == "a" * 64
        assert stored.quota_cost == 7
        assert stored.quota_remaining == 8123


def test_reserve_guard_reads_telemetry_rather_than_estimating():
    with session_scope() as session:
        assert reserve_would_be_breached(session, provider="NEVER_CALLED") is False
        _call(session, provider="RICH", remaining=9000)
        assert reserve_would_be_breached(session, provider="RICH") is False
        _call(session, provider="POOR", remaining=42)
        assert reserve_would_be_breached(session, provider="POOR") is True
        assert latest_quota_remaining(session, provider="POOR") == 42


def test_failed_calls_are_still_recorded_and_never_touch_quote_history():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "failrec")
        service = IngestionService(session)
        now = datetime.now(timezone.utc)
        good_call = _call(session)
        service.persist_quote(quote=_quote(as_of=now), market_id=market.id, provider_call=good_call)

        run = start_run(session, provider=PROVIDER, operation="FETCH_QUOTES")
        record_call(
            session,
            run=run,
            metadata=ProviderCallMetadata(
                endpoint_capability="FETCH_QUOTES",
                requested_at=now,
                responded_at=now,
                http_status=500,
                quota_cost=1,
            ),
            success=False,
            error_category="PROVIDER_UNAVAILABLE",
            error_message="connection failed for https://x?apiKey=LEAK123",
        )
        finish_run(session, run=run, status="FAILED")

        rows = session.execute(
            select(PropQuote).where(PropQuote.market_id == market.id)
        ).scalars().all()
        assert len(rows) == 1, "a failed call must not add, alter or remove quote history"

        failed = session.execute(
            select(ProviderCall).where(ProviderCall.success.is_(False))
        ).scalars().all()
        assert failed and "LEAK123" not in (failed[0].error_message or "")
        assert "[redacted-url]" in failed[0].error_message


# --- 24. Legacy synthetic provenance ----------------------------------


def test_synthetic_quotes_get_real_provenance_and_distinct_fingerprints():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "legacy")
        repo = MarketRepository(session)
        now = datetime.now(timezone.utc)
        # Byte-identical quotes: a content hash would have collided these.
        a = repo.add_quote(
            market_id=market.id, sportsbook="DRAFTKINGS", line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now,
        )
        b = repo.add_quote(
            market_id=market.id, sportsbook="DRAFTKINGS", line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now,
        )
        assert a.fingerprint != b.fingerprint
        for row in (a, b):
            assert row.source == SYNTHETIC_SOURCE
            assert row.provider_call_id == SYNTHETIC_CALL_ID
            assert row.as_of_at == row.retrieved_at
        chain = session.get(ProviderCall, SYNTHETIC_CALL_ID)
        assert chain is not None and chain.endpoint_capability == "SYNTHETIC"
        assert session.get(IngestionRun, chain.ingestion_run_id).provider == SYNTHETIC_SOURCE


def test_add_quote_rejects_an_as_of_after_retrieval():
    with session_scope() as session:
        _, _, _, market = _fixture(session, "asofguard")
        repo = MarketRepository(session)
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="must not be later"):
            repo.add_quote(
                market_id=market.id, sportsbook="DRAFTKINGS", line=Decimal("74.5"),
                over_price=-115, under_price=-105, retrieved_at=now,
                as_of_at=now + timedelta(hours=1),
            )


# --- 20. No DB transaction spans network I/O ---------------------------


def test_the_probe_never_holds_a_transaction_open_across_a_provider_call():
    """Phase 3's discipline, restated for market data.

    A network call inside an open transaction pins a connection for the
    whole round trip and, on a hang, holds locks until the socket times
    out. The probe's structure must therefore be: open, write, close --
    then call -- then open, write, close.

    Asserted structurally rather than behaviourally because the failure
    mode is a code shape, and a runtime assertion would need a real
    provider to hang in order to catch it.
    """

    import ast
    import inspect

    from app.marketdata import validation_probe

    tree = ast.parse(inspect.getsource(validation_probe.run_probe))
    network_calls = {"list_events", "raw_event_odds"}

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        opens_session = any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == "session_scope"
            for item in node.items
        )
        if not opens_session:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                name = getattr(inner.func, "attr", None)
                if name in network_calls:
                    offenders.append(name)

    assert offenders == [], (
        f"provider call(s) {offenders} occur inside a session_scope block; "
        "no database transaction may be held open across network I/O"
    )


# --- 22/23. Existing behaviour survives the seam changes ---------------


def test_synthetic_snapshot_behaviour_is_unchanged_after_source_pinning():
    """The Phase 2B path still works, and still sees only SYNTHETIC rows."""

    with session_scope() as session:
        _, _, _, market = _fixture(session, "pin-legacy")
        repo = MarketRepository(session)
        now = datetime.now(timezone.utc)
        for book, line, over, under in (
            ("DRAFTKINGS", "225.5", -115, -105),
            ("FANDUEL", "225.5", -110, -110),
        ):
            repo.add_quote(
                market_id=market.id, sportsbook=book, line=Decimal(line),
                over_price=over, under_price=under, retrieved_at=now - timedelta(hours=1),
            )

        snapshot = MarketSnapshotService(session).build_snapshot(
            market_id=market.id, canonical_sportsbook="DRAFTKINGS", taken_at=now
        )

        assert snapshot.is_valid_canonical_baseline is True
        assert snapshot.canonical_line == Decimal("225.50")
        assert snapshot.number_of_books == 2
        assert snapshot.canonical_over_probability is not None

        # A season pinned to a real provider sees none of these rows.
        assert (
            MarketSnapshotService(session, market_data_provider="THE_ODDS_API")
            .build_snapshot(
                market_id=market.id, canonical_sportsbook="DRAFTKINGS", taken_at=now
            )
            .is_valid_canonical_baseline
            is False
        )


def test_season_rules_pin_defaults_to_synthetic_not_a_real_vendor():
    """A season created without saying which provider must never
    accidentally claim to be backed by real market data."""

    from app.db.models.season import Season
    from app.db.repositories.season_repository import SeasonRepository
    from app.domain.models import Money, SeasonRules as DomainSeasonRules

    with session_scope() as session:
        season = Season(year=2026, name="pin-default", status="ACTIVE")
        session.add(season)
        session.flush()
        rules = SeasonRepository(session).create_season_rules(
            season_id=season.id,
            rules=DomainSeasonRules(
                rules_version=f"pin-default-{uuid.uuid4()}",
                starting_bankroll=Money(1500),
                kelly_fraction=Decimal("0.25"),
                standard_max_bankroll_fraction=Decimal("0.10"),
                exceptional_max_bankroll_fraction=Decimal("0.20"),
                minimum_stake=Money(25),
                stake_increment=Money(25),
            ),
            effective_from=datetime.now(timezone.utc),
        )
        assert rules.market_data_provider == SYNTHETIC_SOURCE
        assert rules.canonical_sportsbook == "DRAFTKINGS"
