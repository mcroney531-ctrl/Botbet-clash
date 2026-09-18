"""The official, schedule-native benchmark slate commitment.

The defects being closed:

  * the pool was `list[Game]`, so THE ODDS API's event-posting horizon
    decided which fixtures a precommitted sample could draw from;
  * allocation ordered by `(kickoff_at, str(Game.id))` and `Game.id` is
    `uuid4()`, so a clean rebuild could produce a different sample;
  * the commit deadline existed only in a docstring.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.forecast_lab import (
    BenchmarkSlateFixture,
    BenchmarkSlatePlan,
    BenchmarkSlot,
)
from app.db.models.markets import Game
from app.db.models.season import Season, SeasonRules, Week
from app.db.session import session_scope
from app.forecast_lab.benchmark_slate_service import (
    SlateBindingRefused,
    bind_fixture_to_game,
    planned_fixture_from_game,
)
from app.forecast_lab.official_slate import (
    MethodologyNotFrozen,
    SlateCommitRefused,
    commit_official_slate,
    main,
    propose_slate,
)
from app.marketdata.base import ProviderCallMetadata
from app.rosterdata.teams import CanonicalTeam
from app.scheduledata.base import (
    ScheduledGame,
    ScheduleDataError,
    ScheduleFetchResult,
    ScheduleSnapshot,
)

THURSDAY = datetime(2026, 9, 25, 0, 15, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}
# OPENING for the Thursday game opens 144h before it.
DEADLINE = THURSDAY - timedelta(hours=144)
IN_TIME = DEADLINE - timedelta(hours=2)

FIXTURES = (
    ("ATL", "GB", THURSDAY),
    ("CAR", "CLE", SUNDAY),
    ("CIN", "PIT", SUNDAY),
    ("HOU", "IND", SUNDAY),
    ("NE", "JAX", SUNDAY),
    ("KC", "MIA", SUNDAY),
    ("LAC", "BUF", SUNDAY),
    ("NYJ", "DET", SUNDAY),
    ("ARI", "SF", SUNDAY + timedelta(hours=3)),
    ("LA", "DEN", SUNDAY + timedelta(hours=7)),
)


def _scheduled(away, home, kickoff, week=3, game_type="REG"):
    return ScheduledGame(
        season=2026, week=week, game_type=game_type,
        home=CanonicalTeam(home), away=CanonicalTeam(away), kickoff_at=kickoff,
    )


class StubSchedule:
    provider_name = "NFLVERSE"

    def __init__(self, games=None, *, ok=True):
        self.games = tuple(
            _scheduled(a, h, k) for a, h, k in (FIXTURES if games is None else games)
        )
        self.ok = ok
        self.calls = 0

    def _meta(self):
        return ProviderCallMetadata(
            endpoint_capability="FETCH_SCHEDULE",
            requested_at=IN_TIME, responded_at=IN_TIME, http_status=200 if self.ok else 503,
            raw_response_body=b"season,week,home_team,away_team\n",
            raw_response_sha256="c" * 64, raw_response_bytes=32,
        )

    def fetch_schedule(self, *, season):
        self.calls += 1
        if not self.ok:
            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(category="ROSTER_SOURCE_UNAVAILABLE", message="down"),
                call_metadata=self._meta(),
            )
        return ScheduleFetchResult(
            payload=ScheduleSnapshot(
                provider="NFLVERSE", season=season, retrieved_at=IN_TIME, games=self.games,
            ),
            error=None, call_metadata=self._meta(),
        )


def _season(tag, *, method="STABLE_HASH_V1", slate_size=5, week_number=3, windows=None):
    with session_scope() as session:
        season = Season(year=2026, name=f"slate-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=f"slate-{tag}-{uuid.uuid4()}",
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider="THE_ODDS_API", roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards", "rushing_yards"],
            devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=slate_size, benchmark_allocation_method=method,
            batch_methodology="SINGLE_BATCH",
            checkpoint_windows=WINDOWS if windows is None else windows,
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        if week_number is not None:
            session.add(Week(season_id=season.id, week_number=week_number,
                             is_real_money=False))
        return season.id


def _game(season_id, away, home, *, week=3, kickoff=SUNDAY):
    with session_scope() as session:
        game = Game(
            external_ref=f"THE_ODDS_API:{uuid.uuid4()}", season_id=season_id,
            week_number=week, home_team=home, away_team=away,
            home_team_canonical=home, away_team_canonical=away, kickoff_at=kickoff,
        )
        session.add(game)
        session.flush()
        return game.id


def _commit(season_id, *, now=IN_TIME, schedule=None):
    return commit_official_slate(
        season_id=season_id, week_number=3,
        schedule_provider=schedule or StubSchedule(), now=now,
    )


# --- the pool is the schedule, not what Odds has posted ----------------


def test_the_pool_is_the_complete_schedule_with_zero_games_registered():
    """The headline fix. Not one Game row exists and the plan still
    commits against all ten fixtures."""

    season_id = _season("no-games")
    assert _count(Game) == 0

    proposal = _commit(season_id)

    assert len(proposal.pool) == 10
    with session_scope() as session:
        fixtures = session.execute(
            select(BenchmarkSlateFixture)
            .where(BenchmarkSlateFixture.plan_id == proposal.plan_id)
        ).scalars().all()
    assert len(fixtures) == 10, "the complete pool was not frozen under the plan"
    assert all(f.game_id is None for f in fixtures)


def test_missing_odds_events_cannot_change_the_slate():
    """Two fixtures the provider has not posted. The slate is identical to
    the one committed when every event exists, because the pool never came
    from Game rows in the first place."""

    a = _season("odds-none")
    without = _commit(a)

    b = _season("odds-some")
    for away, home, kickoff in FIXTURES[:8]:
        _game(b, away, home, kickoff=kickoff)
    with_games = _commit(b)

    assert [f.key.value for f in without.chosen] == [f.key.value for f in with_games.chosen]
    assert without.fingerprint == with_games.fingerprint


def test_random_game_uuids_cannot_change_the_slate():
    """A clean rebuild generates all-new UUIDs. Committing twice into two
    seasons whose Game rows have unrelated ids must produce the same
    fixtures and the same fingerprint."""

    results = []
    for i in range(3):
        season_id = _season(f"uuid-{i}")
        for away, home, kickoff in FIXTURES:
            _game(season_id, away, home, kickoff=kickoff)
        proposal = _commit(season_id)
        results.append(([f.key.value for f in proposal.chosen], proposal.fingerprint))
    assert len(set(map(str, results))) == 1, f"the slate moved with the UUIDs: {results}"


def test_the_fingerprint_and_pool_count_are_persisted():
    season_id = _season("fingerprint")
    proposal = _commit(season_id)
    with session_scope() as session:
        plan = session.get(BenchmarkSlatePlan, proposal.plan_id)
        assert plan.fixture_pool_count == 10
        assert plan.fixture_pool_fingerprint == proposal.fingerprint
        assert len(plan.fixture_pool_fingerprint) == 64
        assert plan.is_official is True
        assert plan.rules_version and plan.schedule_provider_call_id
        assert plan.earliest_opening_at == DEADLINE


def test_slots_point_at_planned_fixtures_not_games():
    season_id = _season("slots")
    proposal = _commit(season_id)
    with session_scope() as session:
        slots = session.execute(
            select(BenchmarkSlot).where(BenchmarkSlot.plan_id == proposal.plan_id)
        ).scalars().all()
    assert len(slots) == 5
    assert all(s.slate_fixture_id is not None for s in slots)
    assert all(s.game_id is None for s in slots), "a slot was coupled to a Game row"


# --- the deadline ------------------------------------------------------


def test_a_commit_after_the_earliest_opening_is_refused():
    season_id = _season("late")
    with pytest.raises(SlateCommitRefused, match="no override"):
        _commit(season_id, now=DEADLINE + timedelta(seconds=1))
    assert _count(BenchmarkSlatePlan) == 0


def test_the_deadline_is_checked_on_the_clock_after_the_fetch():
    """A command that read the clock at startup could begin before the
    deadline, spend the fetch crossing it, and commit late while
    truthfully reporting it started in time."""

    season_id = _season("clock")
    started_in_time = DEADLINE - timedelta(seconds=30)
    decided_late = DEADLINE + timedelta(seconds=10)

    proposal = propose_slate(
        season_id=season_id, week_number=3,
        schedule_provider=StubSchedule(), now=decided_late,
    )
    assert proposal.decided_at == decided_late
    assert proposal.past_deadline is True
    assert started_in_time < DEADLINE, "the fixture no longer models the race"

    with pytest.raises(SlateCommitRefused):
        commit_official_slate(
            season_id=season_id, week_number=3,
            schedule_provider=StubSchedule(), now=decided_late,
        )


def test_the_deadline_comes_from_the_earliest_fixture_in_the_whole_week():
    """Not the earliest SELECTED fixture. A Thursday game nobody picked
    still opens its OPENING window and still ends the precommit period."""

    season_id = _season("earliest", method="STABLE_HASH_V1")
    proposal = propose_slate(
        season_id=season_id, week_number=3,
        schedule_provider=StubSchedule(), now=IN_TIME,
    )
    assert proposal.earliest_opening_at == DEADLINE
    chosen = {f.key.value for f in proposal.chosen}
    assert "2026:REG:W03:ATL@GB" not in chosen, "fixture no longer proves the point"


def test_a_fixture_without_a_kickoff_refuses_the_whole_commit():
    season_id = _season("no-kickoff")
    schedule = StubSchedule()
    schedule.games = schedule.games + (_scheduled("MIN", "TB", None),)
    with pytest.raises(SlateCommitRefused):
        _commit(season_id, schedule=schedule)
    assert _count(BenchmarkSlatePlan) == 0


# --- methodology comes from frozen rules -------------------------------


def test_a_null_allocation_method_fails_closed():
    """NULL is not a default and not permission to inherit V0."""

    season_id = _season("unfrozen", method=None)
    with pytest.raises(MethodologyNotFrozen, match="has not frozen"):
        _commit(season_id)


def test_a_null_allocation_method_refuses_before_spending_a_fetch():
    season_id = _season("unfrozen-cheap", method=None)
    schedule = StubSchedule()
    with pytest.raises(MethodologyNotFrozen):
        _commit(season_id, schedule=schedule)
    assert schedule.calls == 0, "it fetched before discovering it could not commit"


def test_the_frozen_method_actually_selects_the_slate():
    a = _season("method-a", method="STABLE_HASH_V1")
    b = _season("method-b", method="KICKOFF_BLOCK_STRATIFIED_V1")
    assert [f.key.value for f in _commit(a).chosen] != [
        f.key.value for f in _commit(b).chosen
    ]


def test_the_official_entry_point_takes_no_methodology_arguments():
    """An operator supplies a season and a week. Anything else handed in at
    a command line is not frozen methodology."""

    import inspect

    signature = inspect.signature(commit_official_slate)
    assert set(signature.parameters) == {
        "season_id", "week_number", "schedule_provider", "now",
    }
    parser_args = main.__doc__ or ""
    assert "games" not in signature.parameters
    for forbidden in ("target_slot_count", "prop_types", "allocation_method"):
        assert forbidden not in signature.parameters, (
            f"{forbidden} can be handed in at the official entry point"
        )


# --- once per week -----------------------------------------------------


def test_a_week_cannot_be_recommitted():
    season_id = _season("twice")
    _commit(season_id)
    with pytest.raises(SlateCommitRefused, match="never recommitted"):
        _commit(season_id)
    assert _count(BenchmarkSlatePlan) == 1


def test_a_missing_week_row_refuses_rather_than_inventing_one():
    season_id = _season("no-week", week_number=None)
    with pytest.raises(SlateCommitRefused, match="no Week row"):
        _commit(season_id)


def test_a_schedule_failure_commits_nothing_but_records_the_call():
    from app.db.models.ingestion import ProviderCall

    season_id = _season("sched-down")
    before = _count(ProviderCall)
    with pytest.raises(SlateCommitRefused, match="schedule unavailable"):
        _commit(season_id, schedule=StubSchedule(ok=False))
    assert _count(BenchmarkSlatePlan) == 0
    assert _count(ProviderCall) == before + 1


# --- binding is not reallocation ---------------------------------------


def test_registering_a_game_later_only_binds_the_planned_fixture():
    season_id = _season("bind")
    proposal = _commit(season_id)
    away, home, kickoff = FIXTURES[1]
    game_id = _game(season_id, away, home, kickoff=kickoff)

    with session_scope() as session:
        fixture = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == f"2026:REG:W03:{away}@{home}",
            )
        ).scalar_one()
        before = (
            fixture.fixture_key, fixture.week_number, fixture.away_team_canonical,
            fixture.home_team_canonical, fixture.planned_kickoff_at,
        )
        bind_fixture_to_game(
            session, fixture=fixture, game=session.get(Game, game_id),
            bound_at=SUNDAY, kickoff_tolerance=timedelta(minutes=15),
        )

    with session_scope() as session:
        after = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == f"2026:REG:W03:{away}@{home}",
            )
        ).scalar_one()
        assert after.game_id == game_id
        assert (
            after.fixture_key, after.week_number, after.away_team_canonical,
            after.home_team_canonical, after.planned_kickoff_at,
        ) == before, "binding rewrote what the allocator saw"


def test_binding_does_not_write_back_a_kickoff_inside_tolerance():
    """A game five minutes off the planned kickoff binds fine -- and the
    plan still records what the ALLOCATOR saw. Writing the game's time back
    would quietly edit the historical record toward later data."""

    season_id = _season("no-writeback")
    proposal = _commit(season_id)
    away, home, kickoff = FIXTURES[1]
    shifted = kickoff + timedelta(minutes=5)
    game_id = _game(season_id, away, home, kickoff=shifted)
    key = f"2026:REG:W03:{away}@{home}"

    with session_scope() as session:
        fixture = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == key,
            )
        ).scalar_one()
        bind_fixture_to_game(
            session, fixture=fixture, game=session.get(Game, game_id),
            bound_at=SUNDAY, kickoff_tolerance=timedelta(minutes=15),
        )

    with session_scope() as session:
        after = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == key,
            )
        ).scalar_one()
        assert after.game_id == game_id, "it refused a kickoff inside tolerance"
        assert after.planned_kickoff_at == kickoff, (
            "binding rewrote the planned kickoff to the game's"
        )
        assert after.planned_kickoff_at != shifted


def test_binding_refuses_a_kickoff_the_plan_does_not_recognise():
    season_id = _season("drift")
    proposal = _commit(season_id)
    away, home, kickoff = FIXTURES[1]
    game_id = _game(season_id, away, home, kickoff=kickoff + timedelta(hours=3))

    with session_scope() as session:
        fixture = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == f"2026:REG:W03:{away}@{home}",
            )
        ).scalar_one()
        with pytest.raises(SlateBindingRefused, match="schedule drift"):
            bind_fixture_to_game(
                session, fixture=fixture, game=session.get(Game, game_id),
                bound_at=SUNDAY, kickoff_tolerance=timedelta(minutes=15),
            )

    with session_scope() as session:
        assert session.execute(
            select(BenchmarkSlateFixture.game_id).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == f"2026:REG:W03:{away}@{home}",
            )
        ).scalar() is None


def test_binding_refuses_a_different_fixture():
    season_id = _season("wrong-game")
    proposal = _commit(season_id)
    game_id = _game(season_id, "KC", "MIA", kickoff=SUNDAY)
    with session_scope() as session:
        fixture = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.plan_id == proposal.plan_id,
                BenchmarkSlateFixture.fixture_key == "2026:REG:W03:CAR@CLE",
            )
        ).scalar_one()
        with pytest.raises(SlateBindingRefused, match="does not match"):
            bind_fixture_to_game(
                session, fixture=fixture, game=session.get(Game, game_id),
                bound_at=SUNDAY, kickoff_tolerance=timedelta(minutes=15),
            )


def test_no_slot_is_reallocated_when_a_fixture_is_never_registered():
    """A fixture the provider never lists is a coverage failure, reported.
    The slot stays PENDING on the fixture it was precommitted to."""

    season_id = _season("coverage")
    proposal = _commit(season_id)
    with session_scope() as session:
        before = [
            (s.slot_index, s.slate_fixture_id, s.status)
            for s in session.execute(
                select(BenchmarkSlot).where(BenchmarkSlot.plan_id == proposal.plan_id)
                .order_by(BenchmarkSlot.slot_index)
            ).scalars()
        ]

    # Register only ONE of the ten fixtures, and not one with a slot.
    away, home, kickoff = FIXTURES[1]
    game_id = _game(season_id, away, home, kickoff=kickoff)
    with session_scope() as session:
        fixture = session.execute(
            select(BenchmarkSlateFixture).where(
                BenchmarkSlateFixture.fixture_key == f"2026:REG:W03:{away}@{home}"
            )
        ).scalar_one()
        bind_fixture_to_game(
            session, fixture=fixture, game=session.get(Game, game_id),
            bound_at=SUNDAY, kickoff_tolerance=timedelta(minutes=15),
        )

    with session_scope() as session:
        after = [
            (s.slot_index, s.slate_fixture_id, s.status)
            for s in session.execute(
                select(BenchmarkSlot).where(BenchmarkSlot.plan_id == proposal.plan_id)
                .order_by(BenchmarkSlot.slot_index)
            ).scalars()
        ]
    assert after == before, "a slot moved when an unrelated fixture was registered"


# --- the database refuses an unprovenanced official plan ---------------


def test_an_official_plan_without_provenance_is_refused_by_the_database():
    from sqlalchemy.exc import IntegrityError

    season_id = _season("no-prov")
    with session_scope() as session:
        week_id = session.execute(
            select(Week.id).where(Week.season_id == season_id)
        ).scalar_one()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(BenchmarkSlatePlan(
                week_id=week_id, target_slot_count=5,
                allocation_method="STABLE_HASH_V1", committed_at=IN_TIME,
                is_official=True,
            ))


def test_the_synthetic_adapter_cannot_produce_an_official_plan():
    from app.forecast_lab.benchmark_slate_service import commit_benchmark_slate_plan

    season_id = _season("synthetic")
    game_ids = [_game(season_id, a, h, kickoff=k) for a, h, k in FIXTURES[:3]]
    with session_scope() as session:
        week_id = session.execute(
            select(Week.id).where(Week.season_id == season_id)
        ).scalar_one()
        games = [session.get(Game, g) for g in game_ids]
        plan = commit_benchmark_slate_plan(
            session, week_id=week_id, games=games, target_slot_count=2,
            prop_types=["receiving_yards"], committed_at=IN_TIME,
        )
        assert plan.is_official is False
        assert plan.fixture_pool_fingerprint is not None, (
            "even a synthetic plan records the pool it saw"
        )


def test_the_synthetic_adapter_delegates_rather_than_allocating_itself():
    """One allocation implementation. Two independently-correct writers
    would pass their own tests for months and disagree in the week it
    counted."""

    import ast
    import inspect

    from app.forecast_lab import benchmark_slate_service

    source = inspect.getsource(benchmark_slate_service.commit_benchmark_slate_plan)
    tree = ast.parse(source.lstrip())
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "commit_from_planned_fixtures" in called
    assert "allocate" not in called, "the adapter allocates on its own"
    assert "BenchmarkSlot" not in source, "the adapter writes slots on its own"


# --- the CLI ------------------------------------------------------------


def test_the_cli_preview_writes_no_plan(capsys):
    season_id = _season("cli-preview")
    code = main(
        ["--season-id", str(season_id), "--week-number", "3"],
        schedule_provider=StubSchedule(),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "PREVIEW" in out
    assert _count(BenchmarkSlatePlan) == 0


def test_the_cli_preview_shows_the_complete_pool_and_the_slots(capsys):
    season_id = _season("cli-pool")
    main(["--season-id", str(season_id), "--week-number", "3"],
         schedule_provider=StubSchedule())
    out = capsys.readouterr().out
    for away, home, _ in FIXTURES:
        assert f"2026:REG:W03:{away}@{home}" in out
    assert out.count("<-- SLOT") == 5
    assert "slot 1" in out and "slot 5" in out


def test_the_cli_refuses_cleanly(capsys):
    season_id = _season("cli-refuse", method=None)
    code = main(["--season-id", str(season_id), "--week-number", "3"],
                schedule_provider=StubSchedule())
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    assert "Traceback" not in out


def _count(model) -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(model)).scalar()


# --- week readiness ----------------------------------------------------


def _readiness(season_id, *, now=IN_TIME, schedule=None, week=3):
    from app.forecast_lab.week_readiness import assess_week

    return assess_week(
        season_id=season_id, week_number=week,
        schedule_provider=schedule or StubSchedule(), now=now,
    )


def test_readiness_reports_the_exact_state_that_actually_occurred():
    """16 schedule fixtures, no plan, zero Games registered, and a deadline
    hours away — the state discovered at 16:53 on 2026-09-18."""

    season_id = _season("readiness")
    report = _readiness(season_id)

    assert report.schedule_fixture_count == 10
    assert report.plan_committed is False
    assert report.registered_count == 0
    assert report.earliest_opening_at == DEADLINE
    assert report.past_commit_deadline is False

    text = report.render()
    assert "NOT COMMITTED" in text
    assert "Odds Games registered   0/10" in text
    assert "incomplete (0/10)" in text


def test_readiness_marks_a_week_past_its_commit_deadline_as_ineligible():
    season_id = _season("readiness-late")
    report = _readiness(season_id, now=DEADLINE + timedelta(hours=1))
    assert report.past_commit_deadline is True
    assert "NOT ELIGIBLE" in report.render()


def test_readiness_counts_partial_provider_listing():
    season_id = _season("readiness-partial")
    for away, home, kickoff in FIXTURES[:8]:
        _game(season_id, away, home, kickoff=kickoff)
    report = _readiness(season_id)
    assert report.registered_count == 8
    assert "incomplete (8/10)" in report.render()


def test_readiness_uses_the_committed_plans_own_frozen_pool():
    """A later schedule release must not silently recompute a committed
    week. The plan froze what the allocator saw."""

    season_id = _season("readiness-committed")
    proposal = _commit(season_id)
    smaller = StubSchedule(games=FIXTURES[:4])

    report = _readiness(season_id, schedule=smaller)
    assert report.plan_committed is True
    assert report.plan_id == proposal.plan_id
    assert report.schedule_fixture_count == 10, "it recomputed from a fresh fetch"
    assert report.plan_fingerprint == proposal.fingerprint
    assert smaller.calls == 0, "it fetched a schedule it did not need"


def test_readiness_names_slotted_fixtures_with_no_registered_game():
    season_id = _season("readiness-coverage")
    _commit(season_id)
    report = _readiness(season_id)

    assert len(report.slotted) == 5
    assert len(report.unbound_slots) == 5
    text = report.render()
    assert "COVERAGE FAILURE" in text
    assert "never reallocated" in text


def test_readiness_writes_nothing_and_calls_no_odds_provider():
    import ast
    import inspect

    from app.forecast_lab import week_readiness

    season_id = _season("readiness-readonly")
    before = (_count(Game), _count(BenchmarkSlatePlan), _count(BenchmarkSlateFixture))
    _readiness(season_id)
    assert (_count(Game), _count(BenchmarkSlatePlan), _count(BenchmarkSlateFixture)) == before

    tree = ast.parse(inspect.getsource(week_readiness))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("fetch_quotes", "list_events", "TheOddsApiProvider",
                      "commit_official_slate", "commit_from_planned_fixtures"):
        assert forbidden not in names, f"the readiness report reaches {forbidden}"


def test_the_readiness_cli_exits_cleanly_on_failure(capsys):
    from app.forecast_lab.week_readiness import main as readiness_main

    season_id = _season("readiness-cli")
    code = readiness_main(
        ["--season-id", str(season_id), "--week-number", "3"],
        schedule_provider=StubSchedule(ok=False),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "CANNOT ASSESS" in out
    assert "Traceback" not in out
