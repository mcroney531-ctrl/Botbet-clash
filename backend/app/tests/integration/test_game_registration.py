"""Phase 4A.6 — event registration and checkpoint-state inspection.

Registration exists because `official_capture` deliberately will not
rediscover an event: it rebuilds the provider identity from
`Game.external_ref`, so a capture can never be pointed at the wrong game
by a typo. Something still has to create those rows, and mixing that with
paid quote ingestion would make it neither repeatable nor free.

Every provider call here is a stub. Zero credits.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models.markets import CheckpointRun, Game
from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.marketdata.base import ProviderCallMetadata, ProviderFetchResult
from app.marketdata.dto import ProviderEvent, ProviderEventRef
from app.marketdata.game_registration import (
    EventScopeConflict,
    register_game,
    register_week_events,
    render,
    verify_game_scope,
)
from app.rosterdata.teams import CanonicalTeam

PROVIDER = "THE_ODDS_API"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(days=4)
WINDOW = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}


def _event(event_id: str, *, home="Buffalo Bills", away="Detroit Lions", kickoff=KICKOFF):
    return ProviderEvent(
        ref=ProviderEventRef(provider=PROVIDER, external_event_id=event_id),
        sport_key="americanfootball_nfl",
        kickoff_at=kickoff,
        home_team=home,
        away_team=away,
    )


class StubOdds:
    """Returns a fixed event list. Records how many times it was called, so
    a test can prove registration is free AND that a repeat pass does not
    quietly re-fetch."""

    def __init__(self, events, *, ok=True, error=None, quota_cost=0):
        self.events = events
        self.ok = ok
        self.error = error
        self.quota_cost = quota_cost
        self.calls = 0

    def list_events(self, *, sport, window_start, window_end):
        self.calls += 1
        meta = ProviderCallMetadata(
            endpoint_capability="LIST_EVENTS",
            requested_at=NOW,
            responded_at=NOW,
            http_status=200 if self.ok else 500,
            raw_response_body=b"[]",
            raw_response_sha256="f" * 64,
            raw_response_bytes=2,
            quota_cost=self.quota_cost,
            quota_remaining=497,
        )
        if not self.ok:
            from app.marketdata.base import error_result

            return error_result(
                category=self.error or "PROVIDER_UNAVAILABLE",
                message="stub failure",
                call_metadata=meta,
            )
        # `ok` is a derived property (error is None AND payload is not
        # None), not a constructor argument.
        return ProviderFetchResult(payload=list(self.events), error=None, call_metadata=meta)


def _season(tag: str, *, provider=PROVIDER) -> uuid.UUID:
    with session_scope() as session:
        season = Season(year=2026, name=f"reg-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=f"reg-{tag}-{uuid.uuid4()}",
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider=provider, roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards"], devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=5, batch_methodology="SINGLE_BATCH",
            checkpoint_windows=WINDOW,
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        return season.id


def _register(season_id, events, **kw):
    """Register with a schedule that places every supplied fixture in the
    requested week.

    These tests are about get-or-create, scope verification, idempotency
    and concurrency -- not about week verification, which has its own
    section below. Handing them a matching schedule keeps each test about
    one thing.
    """

    stub = kw.pop("stub", None) or StubOdds(events)
    schedule = kw.pop("schedule", None)
    if schedule is None:
        schedule = _schedule_for(events)
    report = register_week_events(
        season_id=season_id, week_number=4,
        window_start=NOW, window_end=NOW + timedelta(days=21),
        odds_provider=stub, schedule_provider=StubSchedule(schedule),
        apply=kw.pop("apply", True), **kw,
    )
    report._stub = stub  # for call-count assertions
    return report


def _schedule_for(events, *, week=4):
    """A week-`week` schedule entry for every mappable fixture in `events`."""

    from app.rosterdata.teams import canonical_from_odds_api

    games = []
    for event in events:
        try:
            home = canonical_from_odds_api(event.home_team)
            away = canonical_from_odds_api(event.away_team)
        except Exception:
            continue  # unmappable: the job refuses it before week resolution
        games.append(_scheduled(away.value, home.value, week=week, kickoff=KICKOFF))
    return _schedule(*games)


# --- the happy path ---------------------------------------------------


def test_registration_creates_games_from_provider_events():
    season_id = _season("create")
    report = _register(season_id, [_event("e1"), _event("e2", home="Kansas City Chiefs", away="Denver Broncos")])

    assert report.events_observed == 2
    assert len(report.created) == 2
    assert report.reused == []
    assert report.conflicts == []

    with session_scope() as session:
        games = session.execute(select(Game).where(Game.season_id == season_id)).scalars().all()
    assert {g.external_ref for g in games} == {"THE_ODDS_API:e1", "THE_ODDS_API:e2"}
    assert {g.week_number for g in games} == {4}
    assert {(g.home_team_canonical, g.away_team_canonical) for g in games} == {
        ("BUF", "DET"), ("KC", "DEN"),
    }


def test_registration_is_free_and_says_so():
    season_id = _season("free")
    report = _register(season_id, [_event("e1")])
    assert report.quota_cost == 0
    assert "/events is free" in render(report)


def test_the_provider_call_is_recorded_even_though_it_is_free():
    """A free call is still a call we made. The audit chain should not have
    holes just because a row happens to cost nothing."""

    from app.db.models.ingestion import ProviderCall

    season_id = _season("provenance")
    _register(season_id, [_event("e1")])

    with session_scope() as session:
        calls = session.execute(
            select(ProviderCall).where(ProviderCall.endpoint_capability == "LIST_EVENTS")
        ).scalars().all()
    assert len(calls) == 1
    assert calls[0].success is True


# --- idempotency and concurrency --------------------------------------


def test_registering_the_same_event_twice_reuses_the_same_game():
    season_id = _season("idem")
    first = _register(season_id, [_event("e1")])
    second = _register(season_id, [_event("e1")])

    assert len(first.created) == 1
    assert len(second.created) == 0 and len(second.reused) == 1
    assert first.created[0].game_id == second.reused[0].game_id

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar()
    assert count == 1


def test_a_partially_registered_week_can_be_re_run_safely():
    """A conflict on one game must not roll back the registrations that
    already succeeded, and the retry must pick up only what is missing."""

    season_id = _season("partial")
    first = _register(season_id, [_event("e1")])
    assert len(first.created) == 1

    second = _register(season_id, [_event("e1"), _event("e2", home="Kansas City Chiefs", away="Denver Broncos")])
    assert len(second.reused) == 1
    assert len(second.created) == 1

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar()
    assert count == 2


def test_concurrent_registration_cannot_duplicate_an_external_ref():
    """Two passes racing both see no row and both INSERT. The unique index
    decides; the loser re-reads and verifies the winner's row rather than
    failing, because the outcome a caller wants is "this event is
    registered" -- and it is."""

    import threading

    # Eight workers, not two. With two, one routinely finished before the
    # other began and the loser took the ordinary "already exists" branch --
    # so the IntegrityError recovery path went unexercised and a mutation
    # that removed it still passed. Real contention is the point of the
    # test, so make contention likely.
    WORKERS = 8

    season_id = _season("race")
    barrier = threading.Barrier(WORKERS)
    results: dict = {}
    lock = threading.Lock()

    def worker(name):
        barrier.wait()
        try:
            outcome = _register(season_id, [_event("e-race")])
        except Exception as exc:  # recorded, not swallowed
            outcome = exc
        with lock:
            results[name] = outcome

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    for name, outcome in results.items():
        assert not isinstance(outcome, Exception), f"{name} raised: {outcome}"

    with session_scope() as session:
        games = session.execute(
            select(Game).where(Game.external_ref == "THE_ODDS_API:e-race")
        ).scalars().all()
    assert len(games) == 1

    created = sum(len(r.created) for r in results.values())
    assert created == 1, "exactly one worker created it"


def test_no_transaction_is_held_across_the_provider_call():
    """The same rule the capture cycle follows: a network wait must never
    sit inside an open transaction."""

    import ast
    import inspect

    from app.marketdata import game_registration

    tree = ast.parse(inspect.getsource(game_registration))
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        rendered = ast.unparse(node)
        if "session_scope()" not in rendered:
            continue
        assert "list_events" not in rendered, "a provider call inside a transaction"


# --- fail closed on scope conflict ------------------------------------


def test_a_scope_mismatch_fails_loudly_and_does_not_relocate_the_row():
    season_id = _season("conflict")
    _register(season_id, [_event("e1")])

    # Same external_ref, different kickoff. Five minutes, deliberately:
    # anything past the 15-minute agreement tolerance is refused by week
    # verification before it reaches the scope check, and this test is
    # about the scope check.
    moved = _event("e1", kickoff=KICKOFF + timedelta(minutes=5))
    report = _register(season_id, [moved])

    assert len(report.conflicts) == 1
    assert "EVENT_SCOPE_CONFLICT" in report.conflicts[0]
    assert "kickoff" in report.conflicts[0]

    with session_scope() as session:
        game = session.execute(
            select(Game).where(Game.external_ref == "THE_ODDS_API:e1")
        ).scalar_one()
    assert game.kickoff_at == KICKOFF, "the existing row was silently moved"


def test_every_scope_field_is_verified_not_just_the_external_ref():
    """Checking only the ref would make the protection cover the FIRST
    write and nothing after it: a row attached to the wrong season would
    then be found and reused by every later, correct run."""

    season_id = _season("scope")
    other_season = _season("scope-other")
    _register(season_id, [_event("e1")])

    with session_scope() as session:
        game = session.execute(
            select(Game).where(Game.external_ref == "THE_ODDS_API:e1")
        ).scalar_one()

        for kwargs in (
            dict(season_id=other_season, week_number=4, home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF),
            dict(season_id=season_id, week_number=5, home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF),
            dict(season_id=season_id, week_number=4, home=CanonicalTeam.KC, away=CanonicalTeam.DET, kickoff_at=KICKOFF),
            dict(season_id=season_id, week_number=4, home=CanonicalTeam.BUF, away=CanonicalTeam.KC, kickoff_at=KICKOFF),
            dict(season_id=season_id, week_number=4, home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF + timedelta(hours=1)),
        ):
            with pytest.raises(EventScopeConflict):
                verify_game_scope(game, **kwargs)

        # The matching scope is accepted, so the guard is not passing by
        # rejecting everything.
        verify_game_scope(
            game, season_id=season_id, week_number=4,
            home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        )


def test_week_zero_is_refused_before_any_row_is_written():
    season_id = _season("week0")
    stub = StubOdds([_event("e1")])
    with pytest.raises(ValueError, match="real NFL week"):
        register_week_events(
            season_id=season_id, week_number=0,
            window_start=NOW, window_end=NOW + timedelta(days=8),
            odds_provider=stub,
        )
    assert stub.calls == 0, "week 0 was refused only after spending a call"


def test_an_unmapped_team_is_reported_not_guessed():
    season_id = _season("unmapped")
    report = _register(season_id, [_event("e1", home="Atlantis Krakens")])

    assert len(report.unmapped_teams) == 1
    assert report.created == []


def test_the_provider_comes_from_season_rules_not_a_flag():
    """A registration run using a different feed from the one the season is
    pinned to would create Game rows no capture could ever refresh."""

    import inspect

    from app.marketdata import game_registration

    source = inspect.getsource(game_registration)
    for flag in ("--market-data-provider", "--canonical-sportsbook", "--provider"):
        assert flag not in source

    season_id = _season("wrong-pin", provider="SOME_OTHER_PROVIDER")
    with pytest.raises(ValueError, match="pinned to"):
        _register(season_id, [_event("e1")])


# --- scope: identity only ---------------------------------------------


def test_registration_writes_nothing_but_games_and_provenance():
    """Registration must not become a second ingestion path. Mixing it with
    paid quote work would make it neither repeatable nor free."""

    from app.db.models.forecast_lab import EvidenceSnapshot
    from app.db.models.markets import MarketSnapshot, Player, PropMarket, PropQuote
    from app.db.models.roster import GamePlayer

    season_id = _season("identity-only")
    _register(season_id, [_event("e1")])

    with session_scope() as session:
        for model in (Player, PropMarket, PropQuote, MarketSnapshot, EvidenceSnapshot, GamePlayer, CheckpointRun):
            count = session.execute(select(func.count()).select_from(model)).scalar()
            assert count == 0, f"registration created {model.__name__} rows"


def test_registration_never_reaches_a_roster_or_quote_path():
    import ast
    import inspect

    from app.marketdata import game_registration

    tree = ast.parse(inspect.getsource(game_registration))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in (
        "fetch_quotes", "fetch_current_roster", "persist_quote", "resolve_and_record",
        "capture_checkpoint", "MarketSnapshotService", "CheckpointRun",
    ):
        assert forbidden not in code, f"registration must not use {forbidden}"


def test_the_registration_helper_has_one_implementation():
    """live_ingest shares it. Two copies of permanent-identity verification
    is exactly the drift this phase keeps removing."""

    import ast
    import inspect

    from app.marketdata import live_ingest

    code = ast.unparse(ast.parse(inspect.getsource(live_ingest)))
    assert "register_game" in code
    assert "EVENT_SCOPE_CONFLICT" not in code, "live_ingest re-implements scope verification"


# --- checkpoint-state inspection --------------------------------------


def test_the_inspector_reports_checkpoint_state():
    """Added after a wrong claim: the inspector reported identity, quotes
    and movement but nothing about CheckpointRun."""

    import io
    from contextlib import redirect_stdout

    from app.marketdata import inspect_ingestion

    season_id = _season("inspect")
    report = _register(season_id, [_event("e1")])
    game_id = report.created[0].game_id

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        inspect_ingestion.main(["--game-id", str(game_id)])
    text = buffer.getvalue()

    assert "checkpoint state" in text
    for checkpoint_type in ("OPENING", "MID", "FINAL"):
        assert checkpoint_type in text


def test_a_missing_checkpoint_reports_NONE_not_MISSED():
    """Historical absence and a recorded MISSED outcome are different
    facts. Collapsing them would let a gap in the record look like a logged
    decision."""

    import io
    from contextlib import redirect_stdout

    from app.marketdata import inspect_ingestion

    season_id = _season("none-vs-missed")
    report = _register(season_id, [_event("e1")])
    game_id = report.created[0].game_id

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        inspect_ingestion.main(["--game-id", str(game_id)])
    text = buffer.getvalue()

    # Asserted on the STATUS LINE, not on substring presence anywhere in
    # the output: the NONE branch's own explanation says "NOT a recorded
    # MISSED outcome", so a bare `"MISSED" not in text` matches the prose
    # rather than the state. Match the rendered shape instead.
    for checkpoint_type in ("OPENING", "MID", "FINAL"):
        assert f"{checkpoint_type:8} NONE — no CheckpointRun row exists" in text
        assert f"{checkpoint_type:8} MISSED" not in text
    assert "historical absence" in text


def test_the_inspector_reports_a_real_missed_row_as_MISSED():
    """The counterpart: a recorded MISSED must not read as absence."""

    import io
    from contextlib import redirect_stdout

    from app.marketdata import inspect_ingestion

    season_id = _season("real-missed")
    report = _register(season_id, [_event("e1")])
    game_id = report.created[0].game_id

    with session_scope() as session:
        session.add(CheckpointRun(
            game_id=game_id, checkpoint_type="FINAL",
            window_start=KICKOFF - timedelta(hours=6),
            window_end=KICKOFF - timedelta(hours=2),
            target_time=KICKOFF - timedelta(hours=3),
            status="MISSED",
        ))

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        inspect_ingestion.main(["--game-id", str(game_id)])
    text = buffer.getvalue()

    assert "FINAL    MISSED" in text
    assert "OPENING  NONE" in text


def test_the_inspector_is_still_read_only():
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


def test_the_duplicate_recovery_branch_works_deterministically():
    """The threaded test above proves the INVARIANT (never two rows) but
    not this BRANCH: with the GIL, two workers routinely fail to collide
    inside the SELECT->INSERT window, so a mutation removing the
    IntegrityError handler still passed two runs in three.

    Forced deterministically instead of hoped for. Under REPEATABLE READ a
    transaction's snapshot is fixed at its first read, so:

        A reads  -> sees nothing, snapshot frozen
        B writes -> commits
        A writes -> A still sees nothing, INSERTs, hits the unique index

    That is exactly the interleaving the handler exists for, and it is
    reproducible rather than lucky.
    """

    from app.db.session import get_session_factory

    season_id = _season("dup-branch")
    event = _event("e-dup")
    ref = event.ref.as_external_ref()

    session_a = get_session_factory()()
    try:
        session_a.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        # A's snapshot is frozen here, before B exists.
        assert session_a.execute(select(Game).where(Game.external_ref == ref)).scalar_one_or_none() is None

        with session_scope() as session_b:
            register_game(
                session_b, event=event, season_id=season_id, week_number=4,
                home=CanonicalTeam.BUF, away=CanonicalTeam.DET,
            )

        # A still cannot see B's row, so it INSERTs and collides.
        game, created = register_game(
            session_a, event=event, season_id=season_id, week_number=4,
            home=CanonicalTeam.BUF, away=CanonicalTeam.DET,
        )
        assert created is False, "A recovered by adopting the winner's row"
        assert game.external_ref == ref
        session_a.commit()
    finally:
        session_a.close()

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Game).where(Game.external_ref == ref)
        ).scalar()
    assert count == 1


# =====================================================================
# WEEK VERIFICATION (Phase 4A.6 correction)
# =====================================================================
#
# The bug: --week-number + --days-ahead stamped every event in a calendar
# window with the operator's week. Run on 2026-09-18 with the arguments
# actually suggested, that would have labelled 15 Week 2 games and 1 Week 3
# game as Week 4 -- and caught ZERO real Week 4 games, since Week 4 runs
# 2026-10-01..10-05.

from app.marketdata.week_resolution import (
    RESOLVER_VERSION,
    WeekOutcome,
    resolve_event_week,
)
from app.scheduledata.base import ScheduledGame, ScheduleFetchResult, ScheduleSnapshot


def _schedule(*games, season=2026, provider="NFLVERSE"):
    return ScheduleSnapshot(
        provider=provider, season=season, retrieved_at=NOW, games=tuple(games),
    )


def _scheduled(away, home, week, *, kickoff=KICKOFF, game_type="REG", season=2026):
    return ScheduledGame(
        season=season, week=week, game_type=game_type,
        home=CanonicalTeam(home), away=CanonicalTeam(away), kickoff_at=kickoff,
    )


class StubSchedule:
    provider_name = "NFLVERSE"

    def __init__(self, snapshot=None, *, ok=True):
        self.snapshot = snapshot
        self.ok = ok
        self.calls = 0

    def _meta(self, ok: bool):
        # Real metadata, because the schedule call is now PERSISTED. A stub
        # returning None would have hidden the fact that provenance was
        # being recorded at all.
        return ProviderCallMetadata(
            endpoint_capability="FETCH_SCHEDULE",
            requested_at=NOW,
            responded_at=NOW,
            http_status=200 if ok else 503,
            raw_response_body=b"season,week,home_team,away_team\n",
            raw_response_sha256="a" * 64,
            raw_response_bytes=32,
        )

    def fetch_schedule(self, *, season):
        self.calls += 1
        if not self.ok:
            from app.scheduledata.base import ScheduleDataError

            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(category="ROSTER_SOURCE_UNAVAILABLE", message="stub down"),
                call_metadata=self._meta(False),
            )
        return ScheduleFetchResult(
            payload=self.snapshot, error=None, call_metadata=self._meta(True)
        )


# --- the pure rule ----------------------------------------------------


def test_the_requested_week_never_overrides_the_schedule():
    """The whole bug in one assertion: asking for week 4 must not make a
    week 2 fixture week 4."""

    schedule = _schedule(_scheduled("DET", "BUF", week=2))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.OTHER_WEEK
    assert resolution.resolved is False
    assert resolution.week == 2, "the real week is reported, not the requested one"


def test_a_matching_week_resolves():
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.MATCHED
    assert resolution.resolved is True
    assert resolution.week == 4


def test_an_unknown_fixture_is_refused_not_guessed():
    schedule = _schedule(_scheduled("KC", "DEN", week=4))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.UNKNOWN_FIXTURE
    assert resolution.week is None


def test_an_ambiguous_fixture_is_refused():
    """An ordered (away, home) pair should be unique within a season.
    Division rivals meet twice but once at each venue."""

    schedule = _schedule(
        _scheduled("DET", "BUF", week=4),
        _scheduled("DET", "BUF", week=9),
    )
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.AMBIGUOUS_FIXTURE


def test_the_reverse_fixture_is_a_different_game():
    """DET @ BUF and BUF @ DET are different fixtures. Matching on an
    unordered pair would collapse a division rivalry's two meetings."""

    schedule = _schedule(_scheduled("BUF", "DET", week=4))  # at DETROIT
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.UNKNOWN_FIXTURE


def test_a_large_kickoff_disagreement_is_refused():
    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET,
        kickoff_at=KICKOFF + timedelta(hours=30),
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.KICKOFF_DISAGREEMENT


def test_a_small_kickoff_difference_is_tolerated():
    """Wide enough for a rounded or provisional broadcast time, and no
    wider: Game.kickoff_at drives the checkpoint windows."""

    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET,
        kickoff_at=KICKOFF + timedelta(minutes=5),
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.MATCHED


def test_a_drift_that_would_move_a_checkpoint_is_refused():
    """The reason the tolerance is 15 minutes rather than 3 hours: a
    correctly-labelled week whose research clock is hours wrong is not an
    acceptable outcome. A FINAL window targeted at kickoff minus three
    hours would fire at a time that means nothing."""

    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET,
        kickoff_at=KICKOFF + timedelta(hours=2, minutes=45),
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.KICKOFF_DISAGREEMENT
    assert resolution.week == 4, "the week was right; the clock was not"


def test_a_scheduled_game_with_no_kickoff_still_resolves():
    """nflverse publishes future games with an empty gametime before the
    broadcast window is set. That must not block week verification."""

    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=None))
    resolution = resolve_event_week(
        home=CanonicalTeam.BUF, away=CanonicalTeam.DET, kickoff_at=KICKOFF,
        requested_week=4, schedule=schedule,
    )
    assert resolution.outcome is WeekOutcome.MATCHED


# --- the job ----------------------------------------------------------


def _register_verified(season_id, events, schedule, *, apply=False, week=4):
    stub = StubOdds(events)
    return register_week_events(
        season_id=season_id, week_number=week,
        window_start=NOW, window_end=NOW + timedelta(days=21),
        odds_provider=stub, schedule_provider=StubSchedule(schedule), apply=apply,
    )


def test_the_original_poisoning_scenario_writes_nothing():
    """The exact shape of the bug: a discovery window full of other weeks'
    games, with week 4 requested."""

    season_id = _season("poison")
    events = [
        _event("wk2-a", home="Buffalo Bills", away="Detroit Lions"),
        _event("wk2-b", home="Kansas City Chiefs", away="Denver Broncos"),
        _event("wk3-a", home="Philadelphia Eagles", away="Dallas Cowboys"),
    ]
    schedule = _schedule(
        _scheduled("DET", "BUF", week=2),
        _scheduled("DEN", "KC", week=2),
        _scheduled("DAL", "PHI", week=3),
    )

    report = _register_verified(season_id, events, schedule, apply=True)

    assert report.created == [] and report.reused == []
    assert len(report.refused) == 3
    assert all("OTHER_WEEK" in r for r in report.refused)

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar()
    assert count == 0, "an event from another week was written"


def test_a_mixed_window_registers_only_the_requested_week():
    season_id = _season("mixed")
    events = [
        _event("wk2", home="Buffalo Bills", away="Detroit Lions"),
        _event("wk4", home="Kansas City Chiefs", away="Denver Broncos"),
    ]
    schedule = _schedule(
        _scheduled("DET", "BUF", week=2),
        _scheduled("DEN", "KC", week=4),
    )

    report = _register_verified(season_id, events, schedule, apply=True)

    assert len(report.created) == 1
    assert report.created[0].external_ref == "THE_ODDS_API:wk4"
    assert report.created[0].week == 4
    assert len(report.refused) == 1

    with session_scope() as session:
        games = session.execute(select(Game).where(Game.season_id == season_id)).scalars().all()
    assert len(games) == 1 and games[0].week_number == 4


def test_the_week_written_comes_from_the_schedule_not_the_request():
    """Belt and braces: even for a matching week, the persisted value is
    the resolved one."""

    season_id = _season("from-schedule")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=True)

    with session_scope() as session:
        game = session.execute(
            select(Game).where(Game.external_ref == "THE_ODDS_API:e1")
        ).scalar_one()
    assert game.week_number == 4 == report.created[0].week


def test_preview_is_the_default_and_writes_nothing():
    season_id = _season("preview")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule)  # apply omitted

    assert report.applied is False
    assert report.created == []
    assert len(report.eligible) == 1
    assert report.eligible[0].game_id is None
    assert report.eligible[0].previewed is True
    assert "PREVIEW" in render(report)

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar()
    assert count == 0


def test_apply_is_explicit():
    season_id = _season("explicit")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))

    _register_verified(season_id, [_event("e1")], schedule, apply=False)
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar() == 0

    _register_verified(season_id, [_event("e1")], schedule, apply=True)
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar() == 1


def test_an_unusable_schedule_registers_nothing():
    """Without an authoritative week there is nothing to verify against,
    and the only safe behaviour is to write nothing -- which is exactly
    what the old code did not do."""

    season_id = _season("no-schedule")
    stub_odds = StubOdds([_event("e1")])
    report = register_week_events(
        season_id=season_id, week_number=4,
        window_start=NOW, window_end=NOW + timedelta(days=21),
        odds_provider=stub_odds, schedule_provider=StubSchedule(None, ok=False), apply=True,
    )

    assert report.failures and "schedule" in report.failures[0]
    assert stub_odds.calls == 0, "the events call ran before the schedule was known"
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar() == 0


def test_repeated_apply_stays_idempotent_with_verification():
    season_id = _season("idem-verified")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))

    first = _register_verified(season_id, [_event("e1")], schedule, apply=True)
    second = _register_verified(season_id, [_event("e1")], schedule, apply=True)

    assert len(first.created) == 1 and len(second.reused) == 1
    assert first.created[0].game_id == second.reused[0].game_id


# --- inspector lease labelling ----------------------------------------


def test_an_expired_lease_is_not_reported_as_active():
    """A successful cycle deletes its lease, but a crashed worker leaves
    one behind until the next claim reclaims it. Calling that abandoned row
    'open' would send someone hunting a worker that is not running."""

    import io
    from contextlib import redirect_stdout

    from app.marketdata import inspect_ingestion
    from app.marketdata.checkpoint_lease import claim_cycle

    season_id = _season("lease-label")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=True)
    game_id = report.created[0].game_id

    claim_cycle(
        game_id=game_id, checkpoint_type="FINAL",
        now=datetime.now(timezone.utc) - timedelta(hours=2),
        duration_seconds=60, owner="crashed-worker",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        inspect_ingestion.main(["--game-id", str(game_id)])
    text = buffer.getvalue()

    assert "1 EXPIRED" in text
    assert "0 ACTIVE" in text
    assert "EXPIRED FINAL" in text
    assert "not blocking" in text


def test_an_active_lease_is_reported_as_active():
    import io
    from contextlib import redirect_stdout

    from app.marketdata import inspect_ingestion
    from app.marketdata.checkpoint_lease import claim_cycle

    season_id = _season("lease-active")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=True)
    game_id = report.created[0].game_id

    claim_cycle(
        game_id=game_id, checkpoint_type="FINAL",
        now=datetime.now(timezone.utc), duration_seconds=3600, owner="live-worker",
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        inspect_ingestion.main(["--game-id", str(game_id)])
    text = buffer.getvalue()

    assert "1 ACTIVE" in text
    assert "ACTIVE  FINAL" in text


# =====================================================================
# SCHEDULE PROVENANCE, PIN, AND THE AUDITED REPAIR (4A.6 closeout)
# =====================================================================


def test_the_schedule_call_that_decided_the_week_is_persisted():
    """Before this, /events was persisted but the schedule fetch that
    actually DECIDED Game.week_number was consumed and thrown away. We
    could prove which event we saw, not which snapshot classified it."""

    from app.db.models.ingestion import ProviderCall

    season_id = _season("sched-prov")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    _register_verified(season_id, [_event("e1")], schedule, apply=True)

    with session_scope() as session:
        call = session.execute(
            select(ProviderCall).where(ProviderCall.endpoint_capability == "FETCH_SCHEDULE")
        ).scalar_one()
    assert call.success is True
    assert call.raw_response_sha256, "the raw hash is the point of the provenance"
    assert call.raw_response_bytes > 0


def test_an_applied_game_links_to_the_exact_call_that_resolved_its_week():
    from app.db.models.ingestion import ProviderCall
    from app.db.models.markets import GameScopeObservation

    season_id = _season("scope-obs")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=True)
    game_id = report.created[0].game_id

    with session_scope() as session:
        obs = session.execute(
            select(GameScopeObservation).where(GameScopeObservation.game_id == game_id)
        ).scalar_one()
        schedule_call = session.get(ProviderCall, obs.schedule_provider_call_id)
        market_call = session.get(ProviderCall, obs.market_provider_call_id)

    assert obs.resolved_week_number == 4
    assert obs.canonical_home == "BUF" and obs.canonical_away == "DET"
    assert obs.resolver_version
    assert schedule_call.endpoint_capability == "FETCH_SCHEDULE"
    assert market_call.endpoint_capability == "LIST_EVENTS"


def test_a_schedule_failure_is_recorded_too():
    """"The schedule was unreachable at 14:03" is itself the answer to why
    a registration pass wrote nothing."""

    from app.db.models.ingestion import ProviderCall

    season_id = _season("sched-fail-prov")
    register_week_events(
        season_id=season_id, week_number=4,
        window_start=NOW, window_end=NOW + timedelta(days=21),
        odds_provider=StubOdds([_event("e1")]),
        schedule_provider=StubSchedule(None, ok=False), apply=True,
    )

    with session_scope() as session:
        call = session.execute(
            select(ProviderCall).where(ProviderCall.endpoint_capability == "FETCH_SCHEDULE")
        ).scalar_one()
    assert call.success is False


def test_preview_records_telemetry_but_no_scope_observations():
    from app.db.models.ingestion import ProviderCall
    from app.db.models.markets import GameScopeObservation

    season_id = _season("preview-telemetry")
    schedule = _schedule(_scheduled("DET", "BUF", week=4))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=False)

    with session_scope() as session:
        calls = session.execute(select(func.count()).select_from(ProviderCall)).scalar()
        games = session.execute(select(func.count()).select_from(Game)).scalar()
        observations = session.execute(
            select(func.count()).select_from(GameScopeObservation)
        ).scalar()

    assert calls == 2, "both the events and schedule calls are audited"
    assert games == 0 and observations == 0
    text = render(report)
    assert "no Game rows written; provider audit telemetry recorded" in text


def test_the_schedule_source_follows_the_frozen_roster_pin():
    """Registration previously built nflverse unconditionally and checked
    only the MARKET pin -- correct for this season by luck, silently wrong
    for any other."""

    from app.marketdata.game_registration import ScheduleSourceUnavailable, _schedule_provider_for
    from app.rosterdata.providers.nflverse import NflverseScheduleProvider

    assert isinstance(_schedule_provider_for("NFLVERSE"), NflverseScheduleProvider)
    with pytest.raises(ScheduleSourceUnavailable, match="frozen roster_data_provider"):
        _schedule_provider_for("SOME_OTHER_SOURCE")


def test_a_wrong_roster_pin_fails_before_any_game_is_written():
    season_id = _season("wrong-roster-pin")
    with session_scope() as session:
        rules = session.execute(
            select(SeasonRules).where(SeasonRules.season_id == season_id)
        ).scalar_one()
        rules.roster_data_provider = "SOME_OTHER_SOURCE"

    from app.marketdata.game_registration import ScheduleSourceUnavailable

    stub = StubOdds([_event("e1")])
    with pytest.raises(ScheduleSourceUnavailable):
        register_week_events(
            season_id=season_id, week_number=4,
            window_start=NOW, window_end=NOW + timedelta(days=21),
            odds_provider=stub, apply=True,
        )
    assert stub.calls == 0
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar() == 0


def test_kickoff_drift_is_recorded_on_the_observation():
    season_id = _season("drift-obs")
    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    events = [_event("e1", kickoff=KICKOFF + timedelta(minutes=6))]
    report = _register_verified(season_id, events, schedule, apply=True)

    from app.db.models.markets import GameScopeObservation

    with session_scope() as session:
        obs = session.execute(
            select(GameScopeObservation)
            .where(GameScopeObservation.game_id == report.created[0].game_id)
        ).scalar_one()
    assert obs.kickoff_drift_seconds == 360


def test_a_refused_drift_still_appears_in_the_preview_table():
    """A preview that hid the outliers would hide exactly the cases the
    tolerance was chosen to exclude."""

    season_id = _season("drift-table")
    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    events = [_event("e1", kickoff=KICKOFF + timedelta(hours=2))]
    report = _register_verified(season_id, events, schedule, apply=False)

    assert report.eligible == []
    assert len(report.drift) == 1
    assert report.drift[0].refused is True
    text = render(report)
    assert "kickoff agreement" in text
    assert "REFUSED" in text
    assert "7200s" in text


def test_a_game_accepted_outside_tolerance_can_never_reach_a_checkpoint():
    """Because it is never persisted at all: no Game row, so no
    CheckpointRun can be computed from its kickoff."""

    season_id = _season("no-checkpoint")
    schedule = _schedule(_scheduled("DET", "BUF", week=4, kickoff=KICKOFF))
    events = [_event("e1", kickoff=KICKOFF + timedelta(hours=3))]
    _register_verified(season_id, events, schedule, apply=True)

    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Game).where(Game.season_id == season_id)
        ).scalar() == 0
        assert session.execute(
            select(func.count()).select_from(CheckpointRun)
        ).scalar() == 0


def _count(model) -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(model)).scalar()


# --- the audited repair -----------------------------------------------
#
# The repair used to take `--authoritative-week 2` and trust it. That
# reproduced, inside the repair tool, the exact defect the repair exists to
# undo: Phase 4A.2 wrote week 3 because a human typed 3. These tests pin
# the property that replaced it -- THE SCHEDULE DECIDES -- and the lock
# that makes the write safe against a concurrent one.


def _mis_scoped_game(tag: str, *, week=3):
    """A game registered under a week the schedule would not give it.

    Registered with a schedule that agrees at registration time (so the row
    is created normally), then repaired against a schedule that says
    something else -- which is the real history: the week was hand-typed
    before schedule verification existed.
    """

    season_id = _season(f"repair-{tag}")
    schedule = _schedule(_scheduled("DET", "BUF", week=week))
    report = _register_verified(season_id, [_event("e1")], schedule, apply=True, week=week)
    return report.created[0].game_id


def _truth(week=2, *, kickoff=KICKOFF):
    """The authoritative schedule the repair consults."""

    return StubSchedule(_schedule(_scheduled("DET", "BUF", week=week, kickoff=kickoff)))


def _verdict(game_id, *, truth=None):
    from app.services.repair_game_week import derive_authoritative_week

    return derive_authoritative_week(
        game_id=game_id, schedule_provider=truth or _truth(),
    )


# --- the schedule decides, not the operator ---------------------------


def test_the_operator_cannot_supply_an_authoritative_week():
    """No flag can name the corrected week. If one existed, the repair
    would just be the original hand-typing with an audit row attached."""

    from app.services.repair_game_week import main

    with pytest.raises(SystemExit):
        main(["--game-id", str(uuid.uuid4()), "--expect-current-week", "3",
              "--authoritative-week", "2"])


def test_a_refusal_exits_cleanly_instead_of_raising(capsys):
    """A refusal is this tool's NORMAL outcome -- a stale expected week, an
    unresolvable fixture, a no-op. Letting the exception escape printed a
    traceback, which reads like the tool broke rather than like the guard
    worked."""

    from app.services.repair_game_week import main

    game_id = _mis_scoped_game("clean-exit")
    code = main([
        "--game-id", str(game_id),
        "--expect-current-week", "9",          # the row says 3
        "--expect-authoritative-week", "2",
    ], schedule_provider=_truth())
    out = capsys.readouterr().out

    assert code == 1, "a refusal must carry a non-zero exit code"
    assert "REFUSED" in out
    assert "expected week_number 9 but the row says 3" in out
    assert "Traceback" not in out


def test_a_successful_dry_run_exits_zero(capsys):
    from app.services.repair_game_week import main

    game_id = _mis_scoped_game("clean-exit-ok")
    code = main([
        "--game-id", str(game_id),
        "--expect-current-week", "3",
        "--expect-authoritative-week", "2",
    ], schedule_provider=_truth())
    out = capsys.readouterr().out

    assert code == 0
    assert "DRY RUN" in out
    with session_scope() as session:
        assert session.get(Game, game_id).week_number == 3, "a dry run wrote"


def test_the_corrected_week_comes_from_the_schedule():
    from app.services.repair_game_week import apply_repair

    game_id = _mis_scoped_game("derived")
    verdict = _verdict(game_id, truth=_truth(week=2))
    with session_scope() as session:
        apply_repair(session, verdict=verdict, expected_current_week=3)

    with session_scope() as session:
        assert session.get(Game, game_id).week_number == 2


def test_a_different_schedule_answer_produces_a_different_correction():
    """The corrected value TRACKS the schedule. If it were hard-coded or
    operator-supplied, this test would write 2 like the one above."""

    from app.services.repair_game_week import apply_repair

    game_id = _mis_scoped_game("derived-7")
    verdict = _verdict(game_id, truth=_truth(week=7))
    with session_scope() as session:
        apply_repair(session, verdict=verdict, expected_current_week=3)

    with session_scope() as session:
        assert session.get(Game, game_id).week_number == 7


def test_the_repair_uses_the_same_resolver_as_registration():
    """One matcher, not two. A repair that derived the week by a second
    route could disagree with the rule that classified every other game."""

    import ast
    import inspect

    from app.services import repair_game_week

    tree = ast.parse(inspect.getsource(repair_game_week))
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if node.module == "app.marketdata.week_resolution"
    }
    assert "resolve_fixture" in imported
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_fixture" in called


def test_the_expected_authoritative_week_is_a_guard_not_an_override():
    from app.services.repair_game_week import RepairRefused, plan_repair

    game_id = _mis_scoped_game("guard")
    verdict = _verdict(game_id, truth=_truth(week=2))
    with session_scope() as session:
        with pytest.raises(RepairRefused, match="schedule says week 2"):
            plan_repair(
                session, verdict=verdict, expected_current_week=3,
                expect_authoritative_week=5,
            )

    with session_scope() as session:
        assert session.get(Game, game_id).week_number == 3


def test_a_matching_guard_lets_the_repair_through():
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("guard-ok")
    verdict = _verdict(game_id, truth=_truth(week=2))
    with session_scope() as session:
        plan = plan_repair(
            session, verdict=verdict, expected_current_week=3,
            expect_authoritative_week=2,
        )
    assert plan.safe is True
    assert plan.verdict.authoritative_week == 2


def test_an_unresolvable_fixture_is_never_corrected():
    """UNKNOWN_FIXTURE means the schedule cannot confirm any week. Falling
    back to an operator guess there is the whole failure mode."""

    from app.services.repair_game_week import RepairRefused, plan_repair

    game_id = _mis_scoped_game("unknown")
    truth = StubSchedule(_schedule(_scheduled("KC", "DEN", week=2)))
    verdict = _verdict(game_id, truth=truth)
    with session_scope() as session:
        with pytest.raises(RepairRefused, match="did not resolve"):
            plan_repair(session, verdict=verdict, expected_current_week=3)


def test_a_kickoff_disagreement_blocks_the_repair():
    """`Game.kickoff_at` is NOT corrected here, and it drives the checkpoint
    windows. Relabelling the week while leaving a clock the schedule
    disputes would be half a repair."""

    from app.services.repair_game_week import RepairRefused, plan_repair

    game_id = _mis_scoped_game("drift")
    truth = _truth(week=2, kickoff=KICKOFF + timedelta(hours=3))
    verdict = _verdict(game_id, truth=truth)
    assert verdict.resolution.outcome is WeekOutcome.KICKOFF_DISAGREEMENT
    assert verdict.authoritative_week is None, "a disputed kickoff still yielded a week"
    with session_scope() as session:
        with pytest.raises(RepairRefused, match="did not resolve"):
            plan_repair(session, verdict=verdict, expected_current_week=3)


def test_a_small_kickoff_difference_still_repairs():
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("drift-ok")
    verdict = _verdict(game_id, truth=_truth(week=2, kickoff=KICKOFF + timedelta(minutes=10)))
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)
    assert plan.safe is True
    assert plan.verdict.drift_seconds == 600


def test_an_unreachable_schedule_refuses_before_anything_is_planned():
    from app.services.repair_game_week import RepairRefused, derive_authoritative_week

    game_id = _mis_scoped_game("sched-down")
    with pytest.raises(RepairRefused, match="schedule unavailable"):
        derive_authoritative_week(game_id=game_id, schedule_provider=StubSchedule(None, ok=False))


# --- provenance -------------------------------------------------------


def test_the_correction_links_the_exact_schedule_call_that_decided_it():
    from app.db.models.ingestion import ProviderCall
    from app.db.models.markets import GameScopeCorrection
    from app.services.repair_game_week import apply_repair

    game_id = _mis_scoped_game("prov")
    verdict = _verdict(game_id)
    with session_scope() as session:
        apply_repair(session, verdict=verdict, expected_current_week=3)

    with session_scope() as session:
        correction = session.execute(
            select(GameScopeCorrection).where(GameScopeCorrection.game_id == game_id)
        ).scalar_one()
        assert correction.schedule_provider_call_id == verdict.schedule_provider_call_id
        call = session.get(ProviderCall, correction.schedule_provider_call_id)
        assert call is not None, "the correction points at a call that does not exist"
        assert call.endpoint_capability == "FETCH_SCHEDULE"
        assert correction.resolver_version == RESOLVER_VERSION


def test_a_failed_schedule_fetch_is_still_recorded():
    """"The schedule was unreachable at 14:03" is itself the answer to why
    a repair wrote nothing."""

    from app.db.models.ingestion import ProviderCall
    from app.services.repair_game_week import RepairRefused, derive_authoritative_week

    game_id = _mis_scoped_game("prov-fail")
    before = _count(ProviderCall)
    with pytest.raises(RepairRefused):
        derive_authoritative_week(game_id=game_id, schedule_provider=StubSchedule(None, ok=False))
    assert _count(ProviderCall) == before + 1


def test_a_week_correction_cannot_have_null_schedule_provenance():
    """Enforced by the DATABASE, not only by the repair tool. The tool is
    not the only thing that can reach this table, and a correction that
    cannot name its schedule snapshot is the hand-typed week it replaces."""

    from sqlalchemy.exc import IntegrityError

    from app.db.models.markets import GameScopeCorrection

    game_id = _mis_scoped_game("null-prov")
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(GameScopeCorrection(
                game_id=game_id, field_corrected="week_number",
                old_value="3", new_value="2",
                schedule_provider_call_id=None,
                resolver_version=RESOLVER_VERSION, reason="smuggled in",
                corrected_at=NOW,
            ))


# --- the lock ---------------------------------------------------------


def test_apply_rechecks_the_week_under_the_lock():
    """The dry run and the apply are separate processes minutes apart.
    Nothing observed in the dry run is trusted at write time."""

    from app.services.repair_game_week import RepairRefused, apply_repair

    game_id = _mis_scoped_game("stale")
    verdict = _verdict(game_id)

    # Somebody else corrects it first.
    with session_scope() as session:
        session.get(Game, game_id).week_number = 2

    with session_scope() as session:
        with pytest.raises(RepairRefused, match="moved while this repair"):
            apply_repair(session, verdict=verdict, expected_current_week=3)


def test_apply_takes_the_row_for_update():
    import ast
    import inspect

    from app.services import repair_game_week

    source = inspect.getsource(repair_game_week.apply_repair)
    tree = ast.parse(source.lstrip())
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "with_for_update" in called, "apply_repair reads the row without locking it"


def test_no_network_happens_inside_the_locked_transaction():
    """The lock is bounded by local queries only. A provider call inside it
    would put an unbounded network wait inside a row lock."""

    import ast
    import inspect

    from app.services import repair_game_week

    tree = ast.parse(inspect.getsource(repair_game_week.apply_repair).lstrip())
    names = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for forbidden in ("fetch_schedule", "derive_authoritative_week", "load_fixture"):
        assert forbidden not in names, f"apply_repair calls {forbidden} inside the lock"


def test_two_concurrent_repairs_produce_exactly_one_correction():
    """The lock serializes them; the loser sees the corrected row and
    refuses rather than appending a second audit trail for the same move."""

    import threading

    from app.db.models.markets import GameScopeCorrection
    from app.db.session import get_session_factory
    from app.services.repair_game_week import RepairRefused, apply_repair

    game_id = _mis_scoped_game("race")
    verdict = _verdict(game_id)

    start = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        session = get_session_factory()()
        try:
            start.wait(timeout=10)
            apply_repair(session, verdict=verdict, expected_current_week=3)
            session.commit()
            result = "applied"
        except RepairRefused:
            session.rollback()
            result = "refused"
        except Exception as exc:  # pragma: no cover - diagnostic
            session.rollback()
            result = f"error:{type(exc).__name__}"
        finally:
            session.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) == ["applied", "refused"], outcomes
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(GameScopeCorrection)
            .where(GameScopeCorrection.game_id == game_id)
        ).scalar() == 1
        assert session.get(Game, game_id).week_number == 2


# --- the dependency census --------------------------------------------


def _market(session, game_id, *, stat="receiving_yards"):
    from app.db.models.markets import Player, PropMarket

    player = Player(external_ref=f"GSIS:{uuid.uuid4()}", name="Test Player")
    session.add(player)
    session.flush()
    market = PropMarket(game_id=game_id, player_id=player.id, stat_type=stat)
    session.add(market)
    session.flush()
    return market


def _snapshot(session, market_id):
    from app.db.models.markets import MarketSnapshot

    snapshot = MarketSnapshot(
        market_id=market_id, taken_at=NOW, canonical_sportsbook="DRAFTKINGS",
        devig_method="PROPORTIONAL_V1", number_of_books=1,
        books_observed=1, stale_books_excluded=0,
        is_valid_canonical_baseline=True,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def test_a_standalone_market_snapshot_does_not_block_the_repair():
    """The first version blocked on ANY MarketSnapshot, which is too blunt:
    a snapshot is a derived read of quotes hanging off game_id, and game_id
    does not change. What makes an artifact unsafe is something having
    COMMITTED to it."""

    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("standalone")
    with session_scope() as session:
        market = _market(session, game_id)
        _snapshot(session, market.id)

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.market_snapshots == 1
    assert plan.census.standalone_snapshots == 1
    assert plan.census.referenced_snapshots == 0
    assert plan.safe is True, plan.blockers


def test_a_snapshot_referenced_by_evidence_does_block_the_repair():
    from app.db.models.forecast_lab import EvidenceSnapshot
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("referenced")
    with session_scope() as session:
        market = _market(session, game_id)
        snapshot = _snapshot(session, market.id)
        session.add(EvidenceSnapshot(
            market_id=market.id, generated_at=NOW, payload={},
            market_snapshot_id=snapshot.id,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.standalone_snapshots == 0
    assert plan.census.snapshots_in_evidence == 1
    assert plan.safe is False
    assert any("evidence snapshots" in b for b in plan.blockers)
    assert any("referenced (DISTINCT union)" in b for b in plan.blockers)


def _evidence(session, market_id, snapshot_id):
    from app.db.models.forecast_lab import EvidenceSnapshot

    row = EvidenceSnapshot(
        market_id=market_id, generated_at=NOW, payload={},
        market_snapshot_id=snapshot_id,
    )
    session.add(row)
    session.flush()
    return row


def _forecast(session, market_id, evidence_id, season_competitor_id):
    from app.db.models.forecast_lab import ForecastObservation

    row = ForecastObservation(
        season_competitor_id=season_competitor_id, market_id=market_id,
        source_type="BENCHMARK", timestamp=NOW,
        model_probability_over=Decimal("0.60000"), confidence=Decimal("7.00"),
        uncertainty="LOW", evidence_snapshot_id=evidence_id,
        research_eligible=True,
    )
    session.add(row)
    session.flush()
    return row


def _week(game_id, number):
    """A real Week row in the game's own season, so a committed artifact can
    name a week that contradicts the correction."""

    from app.db.models.season import Week

    with session_scope() as session:
        season_id = session.get(Game, game_id).season_id
        existing = session.execute(
            select(Week).where(Week.season_id == season_id, Week.week_number == number)
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        week = Week(season_id=season_id, week_number=number, is_real_money=False)
        session.add(week)
        session.flush()
        return week.id


def _competitor(game_id, tag):
    from app.db.models.season import Competitor, SeasonCompetitor

    with session_scope() as session:
        season_id = session.get(Game, game_id).season_id
        cid = f"openai-{tag}"
        session.add(Competitor(id=cid, provider="openai", display_name="GPT"))
        session.flush()
        sc = SeasonCompetitor(
            season_id=season_id, competitor_id=cid,
            model_identifier="gpt-test", model_version="v1",
        )
        session.add(sc)
        session.flush()
        return sc.id


# --- every competitive artifact that can contradict the correction ----
#
# The census used to cover five models and call itself complete. Each test
# below attaches exactly ONE durable artifact to an otherwise-repairable
# game and proves the repair refuses. Without the traversal the census
# reports zero and the repair proceeds, leaving
# `Game.week_number = 2` while the artifact still says Week 3.


def test_a_benchmark_slot_blocks_the_repair():
    from app.db.models.forecast_lab import BenchmarkSlatePlan, BenchmarkSlot
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("slot")
    week_id = _week(game_id, 3)
    with session_scope() as session:
        plan_row = BenchmarkSlatePlan(
            week_id=week_id, target_slot_count=5,
            allocation_method="EVEN", committed_at=NOW,
        )
        session.add(plan_row)
        session.flush()
        session.add(BenchmarkSlot(
            plan_id=plan_row.id, slot_index=0, game_id=game_id,
            target_stat_type="receiving_yards",
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.benchmark_slots == 1
    assert plan.census.committed_weeks["benchmark_slots"] == [3]
    assert plan.safe is False
    assert any("benchmark slots" in b for b in plan.blockers)


def test_a_ticket_blocks_the_repair_and_names_its_week():
    """The exact contradiction: correcting the game to week 2 while a
    Ticket on one of its markets still points at Week 3."""

    from app.db.models.competition import Ticket
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("ticket")
    week_id = _week(game_id, 3)
    sc_id = _competitor(game_id, "ticket")
    with session_scope() as session:
        market = _market(session, game_id)
        session.add(Ticket(
            season_competitor_id=sc_id, week_id=week_id, market_id=market.id,
            urgency="STANDARD", side="OVER", risk_posture="STANDARD",
            kelly_reference_stake_cents=100, model_requested_stake_cents=50,
            final_allowed_stake_cents=50, observed_line=Decimal("55.5"),
            why_now="test",
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.tickets == 1
    assert plan.census.committed_weeks["tickets"] == [3], "the census hid the contradiction"
    assert plan.safe is False
    assert any("tickets" in b and "week(s) [3]" in b for b in plan.blockers)


def test_a_wager_and_its_money_block_the_repair():
    from app.db.models.competition import Ticket, Wager
    from app.db.models.settlement import BankrollTransaction
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("wager")
    week_id = _week(game_id, 3)
    sc_id = _competitor(game_id, "wager")
    with session_scope() as session:
        market = _market(session, game_id)
        ticket = Ticket(
            season_competitor_id=sc_id, week_id=week_id, market_id=market.id,
            urgency="STANDARD", side="OVER", risk_posture="STANDARD",
            kelly_reference_stake_cents=100, model_requested_stake_cents=50,
            final_allowed_stake_cents=50, observed_line=Decimal("55.5"),
            why_now="test",
        )
        session.add(ticket)
        session.flush()
        wager = Wager(
            ticket_id=ticket.id, season_competitor_id=sc_id, week_id=week_id,
            market_id=market.id, requested_stake_cents=50, execution_status="PLACED",
        )
        session.add(wager)
        session.flush()
        session.add(BankrollTransaction(
            season_competitor_id=sc_id, week_id=week_id, wager_id=wager.id,
            type="STAKE", amount_cents=-50,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.wagers == 1
    assert plan.census.bankroll_transactions == 1, "real money moved and the census missed it"
    assert plan.census.committed_weeks["wagers"] == [3]
    assert plan.safe is False


def test_a_pass_decision_on_this_market_blocks_the_repair():
    from app.db.models.competition import PassDecision
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("pass")
    week_id = _week(game_id, 3)
    sc_id = _competitor(game_id, "pass")
    with session_scope() as session:
        market = _market(session, game_id)
        session.add(PassDecision(
            season_competitor_id=sc_id, week_id=week_id,
            best_available_candidate_market_id=market.id,
            reason_for_pass="no edge",
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.pass_decisions == 1
    assert plan.census.committed_weeks["pass_decisions"] == [3]
    assert plan.safe is False


def test_a_research_settlement_blocks_the_repair():
    from app.db.models.settlement import ResearchSettlement
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("settled")
    with session_scope() as session:
        market = _market(session, game_id)
        session.add(ResearchSettlement(
            market_id=market.id, research_stat_value_at_lock=Decimal("61.00"),
            research_locked_at=NOW,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.research_settlements == 1
    assert plan.safe is False


def test_a_stake_recommendation_blocks_the_repair():
    from app.db.models.competition import StakeRecommendation
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("stake")
    sc_id = _competitor(game_id, "stake")
    with session_scope() as session:
        market = _market(session, game_id)
        snapshot = _snapshot(session, market.id)
        evidence = _evidence(session, market.id, snapshot.id)
        observation = _forecast(session, market.id, evidence.id, sc_id)
        session.add(StakeRecommendation(
            season_competitor_id=sc_id, market_id=market.id,
            forecast_observation_id=observation.id,
            bankroll_at_decision_cents=1500,
            model_probability_over=Decimal("0.60000"),
            kelly_fraction_used=Decimal("0.2000"),
            kelly_reference_stake_cents=100,
            model_requested_stake_cents=50,
            risk_posture="STANDARD", final_allowed_stake_cents=50,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.stake_recommendations == 1
    assert plan.safe is False


# --- batched agent sessions -------------------------------------------


def test_a_batched_agent_session_is_detected_through_the_join_table():
    """`create_pending` populates ONLY `agent_session_evidence_snapshots`
    and leaves both singular columns NULL, so counting those two columns
    reported zero agent sessions for every batched call. This test attaches
    a session exactly the way the real batch path does."""

    from app.db.models.forecast_lab import AgentSession, AgentSessionEvidenceSnapshot
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("batched")
    sc_id = _competitor(game_id, "batched")
    with session_scope() as session:
        market = _market(session, game_id)
        snapshot = _snapshot(session, market.id)
        evidence = _evidence(session, market.id, snapshot.id)
        agent = AgentSession(
            season_competitor_id=sc_id, provider="openai",
            model_identifier="gpt-test", call_type="BENCHMARK_FORECAST",
            prompt_version="v1", schema_version="v1", status="PENDING",
            orchestration_key=f"k-{uuid.uuid4()}",
            bankroll_at_decision_cents=1500, timestamp=NOW,
        )
        session.add(agent)
        session.flush()
        assert agent.evidence_snapshot_id is None
        assert agent.market_snapshot_id is None
        session.add(AgentSessionEvidenceSnapshot(
            agent_session_id=agent.id, evidence_snapshot_id=evidence.id,
            market_id=market.id,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.agent_sessions == 1, "a batched model call was invisible to the census"
    assert plan.census.batched_agent_sessions == 1
    assert plan.safe is False
    assert any("agent sessions" in b for b in plan.blockers)


def test_one_session_reachable_two_ways_is_counted_once():
    from app.db.models.forecast_lab import AgentSession, AgentSessionEvidenceSnapshot
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("double-route")
    sc_id = _competitor(game_id, "double-route")
    with session_scope() as session:
        market = _market(session, game_id)
        snapshot = _snapshot(session, market.id)
        evidence = _evidence(session, market.id, snapshot.id)
        agent = AgentSession(
            season_competitor_id=sc_id, provider="openai",
            model_identifier="gpt-test", call_type="BENCHMARK_FORECAST",
            prompt_version="v1", schema_version="v1", status="PENDING",
            orchestration_key=f"k-{uuid.uuid4()}",
            bankroll_at_decision_cents=1500, timestamp=NOW,
            evidence_snapshot_id=evidence.id, market_snapshot_id=snapshot.id,
        )
        session.add(agent)
        session.flush()
        session.add(AgentSessionEvidenceSnapshot(
            agent_session_id=agent.id, evidence_snapshot_id=evidence.id,
            market_id=market.id,
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.agent_sessions == 1, "the same session counted more than once"


# --- the snapshot union -----------------------------------------------


def test_disjoint_reference_sets_are_unioned_not_maxed():
    """max(2, 3) = 3, but five distinct snapshots are referenced. The old
    property understated referenced_snapshots and therefore OVERSTATED
    standalone_snapshots -- the number the safe/unsafe call reads."""

    from app.db.models.forecast_lab import AgentSession
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("union")
    sc_id = _competitor(game_id, "union")
    with session_scope() as session:
        market = _market(session, game_id)
        # Two snapshots referenced only by evidence.
        for _ in range(2):
            snap = _snapshot(session, market.id)
            _evidence(session, market.id, snap.id)
        # Three DIFFERENT snapshots referenced only by agent sessions.
        for _ in range(3):
            snap = _snapshot(session, market.id)
            session.add(AgentSession(
                season_competitor_id=sc_id, provider="openai",
                model_identifier="gpt-test", call_type="BENCHMARK_FORECAST",
                prompt_version="v1", schema_version="v1", status="PENDING",
                orchestration_key=f"k-{uuid.uuid4()}",
                bankroll_at_decision_cents=1500, timestamp=NOW,
                market_snapshot_id=snap.id,
            ))
        # One referenced by nothing.
        _snapshot(session, market.id)

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    c = plan.census
    assert c.market_snapshots == 6
    assert (c.snapshots_in_evidence, c.snapshots_in_agent_sessions) == (2, 3)
    assert c.referenced_snapshots == 5, "max(2,3)=3 is a LOWER bound on the union"
    assert c.standalone_snapshots == 1


# --- completeness -----------------------------------------------------


def test_every_table_that_can_reach_a_game_is_classified():
    """The census's own completeness check, computed from the live
    metadata rather than a hand-kept list. A model added later that can
    reach a game turns this red until someone classifies it -- which is
    the only durable defence against the gap that made this pass
    necessary. It already caught ingestion_runs and provider_calls."""

    import app.db.models  # noqa: F401  -- register every mapper
    from app.services.repair_game_week import (
        CENSUS_CLASSIFICATION,
        tables_reaching_a_game,
    )

    reachable = tables_reaching_a_game()
    assert len(reachable) > 20, "the FK closure collapsed; it is not proving anything"
    unclassified = reachable - set(CENSUS_CLASSIFICATION)
    assert not unclassified, (
        f"these tables can reach a game but the census does not classify them: "
        f"{sorted(unclassified)}"
    )
    stale = set(CENSUS_CLASSIFICATION) - reachable
    assert not stale, f"classified but unreachable: {sorted(stale)}"


def test_every_blocking_table_is_actually_queried():
    """A classification that nothing queries is a comment, not a guard."""

    import ast
    import inspect

    from app.services import repair_game_week
    from app.services.repair_game_week import CENSUS_CLASSIFICATION, Reach

    source = inspect.getsource(repair_game_week.take_census)
    names = {
        n.id for n in ast.walk(ast.parse(source.lstrip())) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(ast.parse(source.lstrip())) if isinstance(n, ast.Attribute)
    }
    # table name -> the model class the census must reference
    models = {
        "evidence_snapshots": "EvidenceSnapshot",
        "forecast_observations": "ForecastObservation",
        "agent_sessions": "AgentSession",
        "agent_session_evidence_snapshots": "AgentSessionEvidenceSnapshot",
        "research_settlements": "ResearchSettlement",
        "benchmark_slots": "BenchmarkSlot",
        "benchmark_slate_fixtures": "BenchmarkSlateFixture",
        "benchmark_slate_plans": "BenchmarkSlatePlan",
        "stake_recommendations": "StakeRecommendation",
        "tickets": "Ticket",
        "wagers": "Wager",
        "pass_decisions": "PassDecision",
        "settlements": "Settlement",
        "bankroll_transactions": "BankrollTransaction",
    }
    blocking = {t for t, r in CENSUS_CLASSIFICATION.items() if r is Reach.BLOCKING}
    assert blocking == set(models), (
        "the blocking set changed; update this test's model map deliberately"
    )
    for table, model in models.items():
        assert model in names, f"{table} is classified BLOCKING but take_census never queries it"


def test_a_repairable_game_has_only_observation():
    """The expected DET shape: raw quotes plus standalone acceptance
    snapshots, and nothing committed."""

    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("clean")
    with session_scope() as session:
        market = _market(session, game_id)
        _snapshot(session, market.id)
        _snapshot(session, market.id)

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.safe is True, plan.blockers
    assert plan.census.standalone_snapshots == 2
    assert plan.census.referenced_snapshots == 0
    assert [r.label for r in plan.census.rows() if r.blocking] == []


def test_the_render_shows_every_blocking_table_even_at_zero():
    """"We looked and found none" and "we never looked" must not render
    identically. Every counted artifact appears whatever its count."""

    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("render")
    verdict = _verdict(game_id)
    with session_scope() as session:
        text = plan_repair(session, verdict=verdict, expected_current_week=3).render()

    for label in ("tickets", "wagers", "pass decisions", "benchmark slots",
                  "research settlements", "stake recommendations", "settlements",
                  "bankroll transactions", "agent sessions (any route)"):
        assert label in text, f"the census report never mentions {label}"


def test_the_census_reports_checkpoints_by_status():
    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("census-cp")
    with session_scope() as session:
        for checkpoint, status in (("OPENING", "MISSED"), ("MID", "PENDING")):
            session.add(CheckpointRun(
                game_id=game_id, checkpoint_type=checkpoint,
                window_start=KICKOFF - timedelta(hours=6),
                window_end=KICKOFF - timedelta(hours=2),
                target_time=KICKOFF - timedelta(hours=3),
                status=status,
            ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)

    assert plan.census.checkpoints == {"MISSED": ["OPENING"], "PENDING": ["MID"]}
    assert plan.census.captured_checkpoints == []
    assert plan.safe is True


def test_the_repair_refuses_when_a_captured_checkpoint_depends_on_it():
    """At that point it is no longer a mislabelled attribute; it is a
    dependency graph, and a human needs to see it."""

    from app.services.repair_game_week import RepairRefused, apply_repair, plan_repair

    game_id = _mis_scoped_game("captured")
    with session_scope() as session:
        session.add(CheckpointRun(
            game_id=game_id, checkpoint_type="FINAL",
            window_start=KICKOFF - timedelta(hours=6),
            window_end=KICKOFF - timedelta(hours=2),
            target_time=KICKOFF - timedelta(hours=3),
            status="CAPTURED", captured_at=KICKOFF - timedelta(hours=3),
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)
        assert plan.safe is False
        assert any("CAPTURED" in b for b in plan.blockers)
        with pytest.raises(RepairRefused):
            apply_repair(session, verdict=verdict, expected_current_week=3)

    with session_scope() as session:
        assert session.get(Game, game_id).week_number == 3, "refused but still changed it"


def test_a_pending_checkpoint_does_not_block_the_repair():
    """Only artifacts whose INTERPRETATION would change. A PENDING row
    holds no research content."""

    from app.services.repair_game_week import plan_repair

    game_id = _mis_scoped_game("pending")
    with session_scope() as session:
        session.add(CheckpointRun(
            game_id=game_id, checkpoint_type="FINAL",
            window_start=KICKOFF - timedelta(hours=6),
            window_end=KICKOFF - timedelta(hours=2),
            target_time=KICKOFF - timedelta(hours=3),
            status="PENDING",
        ))

    verdict = _verdict(game_id)
    with session_scope() as session:
        plan = plan_repair(session, verdict=verdict, expected_current_week=3)
    assert plan.safe is True


# --- the invariants that outlive this one repair ----------------------


def test_the_repair_refuses_without_the_expected_current_week():
    from app.services.repair_game_week import RepairRefused, plan_repair

    game_id = _mis_scoped_game("expect")
    verdict = _verdict(game_id)
    with session_scope() as session:
        with pytest.raises(RepairRefused, match="expected week_number"):
            plan_repair(session, verdict=verdict, expected_current_week=9)


def test_the_repair_refuses_a_no_op():
    from app.services.repair_game_week import RepairRefused, plan_repair

    game_id = _mis_scoped_game("noop")
    verdict = _verdict(game_id, truth=_truth(week=3))
    with session_scope() as session:
        with pytest.raises(RepairRefused, match="nothing to correct"):
            plan_repair(session, verdict=verdict, expected_current_week=3)


def test_the_repair_is_audited_and_changes_only_the_week():
    from app.db.models.markets import GameScopeCorrection
    from app.services.repair_game_week import apply_repair

    game_id = _mis_scoped_game("audited")
    with session_scope() as session:
        before = session.get(Game, game_id)
        snapshot = (
            before.external_ref, before.home_team_canonical,
            before.away_team_canonical, before.kickoff_at,
            before.season_id, before.home_team, before.away_team, before.status,
        )

    verdict = _verdict(game_id)
    with session_scope() as session:
        apply_repair(session, verdict=verdict, expected_current_week=3)

    with session_scope() as session:
        after = session.get(Game, game_id)
        correction = session.execute(
            select(GameScopeCorrection).where(GameScopeCorrection.game_id == game_id)
        ).scalar_one()

        assert after.week_number == 2
        assert (
            after.external_ref, after.home_team_canonical,
            after.away_team_canonical, after.kickoff_at,
            after.season_id, after.home_team, after.away_team, after.status,
        ) == snapshot, "the repair touched something other than the week"
        assert after.id == game_id
    assert correction.field_corrected == "week_number"
    assert (correction.old_value, correction.new_value) == ("3", "2")
    assert correction.reason and correction.resolver_version


def test_the_repair_never_touches_identity_fields():
    import ast
    import inspect

    from app.services import repair_game_week

    tree = ast.parse(inspect.getsource(repair_game_week))
    assigned = {
        t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Attribute)
    }
    for forbidden in ("external_ref", "home_team_canonical", "away_team_canonical",
                      "kickoff_at", "id", "season_id"):
        assert forbidden not in assigned, f"the repair assigns Game.{forbidden}"
    assert "week_number" in assigned


def test_the_repair_creates_no_markets_quotes_or_checkpoints():
    from app.db.models.markets import PropQuote

    game_id = _mis_scoped_game("no-writes")
    before = (_count(PropQuote), _count(CheckpointRun))

    from app.services.repair_game_week import apply_repair

    verdict = _verdict(game_id)
    with session_scope() as session:
        apply_repair(session, verdict=verdict, expected_current_week=3)

    assert (_count(PropQuote), _count(CheckpointRun)) == before
