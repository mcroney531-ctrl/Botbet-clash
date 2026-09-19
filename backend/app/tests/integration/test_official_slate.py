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
from app.domain.week_profile import WeekProfile, flags_for
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

# A realistic SIXTEEN-fixture week: one Thursday, nine early Sunday, two
# late-afternoon, two more, one Sunday night, one Monday night. Sixteen and
# not ten, because a ten-slot slate over a ten-fixture week selects
# EVERYTHING and every coverage assertion below becomes vacuous.
FIXTURES = (
    ("ATL", "GB", THURSDAY),
    ("CAR", "CLE", SUNDAY),
    ("CIN", "PIT", SUNDAY),
    ("HOU", "IND", SUNDAY),
    ("NE", "JAX", SUNDAY),
    ("KC", "MIA", SUNDAY),
    ("LAC", "BUF", SUNDAY),
    ("NYJ", "DET", SUNDAY),
    ("SEA", "WAS", SUNDAY),
    ("TEN", "NYG", SUNDAY),
    ("ARI", "SF", SUNDAY + timedelta(hours=3)),
    ("MIN", "TB", SUNDAY + timedelta(hours=3)),
    ("BAL", "DAL", SUNDAY + timedelta(hours=3, minutes=25)),
    ("LV", "NO", SUNDAY + timedelta(hours=3, minutes=25)),
    ("LA", "DEN", SUNDAY + timedelta(hours=7)),
    ("PHI", "CHI", SUNDAY + timedelta(days=1, hours=7)),
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


# The PRODUCTION slot count (CONSTITUTION.md §20, RULES.md §7). This helper
# builds production-shaped seasons, so it must not encode a number no
# governing document specifies -- reviewing the allocators at five slots
# is what made the first Phase 4A.7 approval package unsound.
PRODUCTION_SLATE_SIZE = 10


def _season(tag, *, method="STABLE_HASH_V1", slate_size=PRODUCTION_SLATE_SIZE,
            week_number=3, windows=None):
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
            # A REVIEWED profile. Setting is_real_money alone leaves
            # standings and awards at their column default of True, which is
            # the half-rehearsal state the week-profile work exists to make
            # unreachable -- and readiness correctly reports it NONSTANDARD.
            flags = flags_for(WeekProfile.REHEARSAL)
            session.add(Week(
                season_id=season.id, week_number=week_number,
                is_real_money=flags.is_real_money,
                counts_toward_standings=flags.counts_toward_standings,
                counts_toward_awards=flags.counts_toward_awards,
            ))
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

    assert len(proposal.pool) == 16
    with session_scope() as session:
        fixtures = session.execute(
            select(BenchmarkSlateFixture)
            .where(BenchmarkSlateFixture.plan_id == proposal.plan_id)
        ).scalars().all()
    assert len(fixtures) == 16, "the complete pool was not frozen under the plan"
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
        assert plan.fixture_pool_count == 16
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
    assert len(slots) == PRODUCTION_SLATE_SIZE
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
    """The pinned name drives selection, not a default buried in the core."""

    from app.forecast_lab.slate_allocation import allocate

    season_id = _season("method-drives")
    proposal = _commit(season_id)
    expected = allocate(proposal.pool, slots=PRODUCTION_SLATE_SIZE, method="STABLE_HASH_V1")
    assert [f.key.value for f in proposal.chosen] == [f.key.value for f in expected]
    with session_scope() as session:
        plan = session.get(BenchmarkSlatePlan, proposal.plan_id)
        assert plan.allocation_method == "STABLE_HASH_V1"


def test_an_implemented_but_unapproved_method_is_refused():
    """Being in the registry is not approval. Every other method there was
    written to be MEASURED against the approved one, and two have named
    defects."""

    season_id = _season("unapproved", method="KICKOFF_BLOCK_STRATIFIED_V1")
    with pytest.raises(MethodologyNotFrozen, match="NOT APPROVED"):
        _commit(season_id)
    assert _count(BenchmarkSlatePlan) == 0


def test_the_rejected_methods_each_carry_a_recorded_defect():
    from app.forecast_lab.slate_allocation import ALLOCATION_METHODS, APPROVED_METHODS

    assert APPROVED_METHODS == {"STABLE_HASH_V1"}
    for name, method in ALLOCATION_METHODS.items():
        if not method.approved:
            assert method.defect, f"{name} is not approved but records no reason"


def test_the_kickoff_block_candidate_structurally_excludes_the_last_block():
    """Its measured "5 of 6 blocks" is always the FIRST five. It walks
    blocks chronologically and stops at `slots`, so Monday night can never
    be selected. Recorded here rather than repaired under the same name:
    a method that was compared has to keep meaning what it meant."""

    from app.forecast_lab.fixture_identity import FixtureKey, PlannedFixture
    from app.forecast_lab.slate_allocation import allocate

    def fx(away, home, offset_hours):
        return PlannedFixture(
            key=FixtureKey(season=2026, game_type="REG", week=3,
                           away=CanonicalTeam(away), home=CanonicalTeam(home)),
            kickoff_at=SUNDAY + timedelta(hours=offset_hours),
        )

    pool = [fx("ATL", "GB", -48)] + [
        fx(a, h, 0) for a, h in (
            ("CAR", "CLE"), ("CIN", "PIT"), ("HOU", "IND"), ("NE", "JAX"),
            ("KC", "MIA"), ("LAC", "BUF"), ("NYJ", "DET"), ("SEA", "WAS"),
        )
    ] + [fx("ARI", "SF", 3), fx("MIN", "TB", 3.25), fx("LA", "DEN", 7),
         fx("PHI", "CHI", 31)]

    blocks = sorted({f.kickoff_at for f in pool})
    assert len(blocks) == 6
    chosen = allocate(pool, slots=5, method="KICKOFF_BLOCK_STRATIFIED_V1")
    picked = {f.kickoff_at for f in chosen}
    assert blocks[-1] not in picked, "the defect is gone; re-review the method"
    assert picked == set(blocks[:5]), "it no longer takes exactly the first five"


# --- the commit transaction trusts nothing observed before it ----------


def test_an_amendment_landing_mid_commit_refuses_the_write():
    """The proposal is built across a network call. An allocation-method
    amendment can land in that gap, and a slate committed under a
    superseded rules_version would claim methodology it never ran under."""

    import app.forecast_lab.official_slate as module

    season_id = _season("amended")
    original = module.propose_slate

    def amend_after_proposal(**kwargs):
        # The gap the guard exists for: the proposal is complete, the
        # write has not started, and an amendment lands.
        proposal = original(**kwargs)
        with session_scope() as session:
            rules = session.execute(
                select(SeasonRules).where(SeasonRules.season_id == season_id)
            ).scalar_one()
            rules.rules_version = f"{rules.rules_version}-amended"
        return proposal

    module.propose_slate = amend_after_proposal
    try:
        with pytest.raises(SlateCommitRefused, match="methodology changed"):
            commit_official_slate(
                season_id=season_id, week_number=3,
                schedule_provider=StubSchedule(), now=IN_TIME,
            )
    finally:
        module.propose_slate = original
    assert _count(BenchmarkSlatePlan) == 0


def test_an_allocation_method_change_mid_commit_refuses_the_write():
    season_id = _season("method-swapped")
    with session_scope() as session:
        pass

    import app.forecast_lab.official_slate as module

    original = module.propose_slate

    def swap_after_proposal(**kwargs):
        proposal = original(**kwargs)
        with session_scope() as session:
            rules = session.execute(
                select(SeasonRules).where(SeasonRules.season_id == season_id)
            ).scalar_one()
            rules.benchmark_allocation_method = "ROUND_ROBIN_BY_KICKOFF_V0"
        return proposal

    module.propose_slate = swap_after_proposal
    try:
        with pytest.raises(SlateCommitRefused, match="allocation method changed"):
            commit_official_slate(
                season_id=season_id, week_number=3,
                schedule_provider=StubSchedule(), now=IN_TIME,
            )
    finally:
        module.propose_slate = original
    assert _count(BenchmarkSlatePlan) == 0


def test_the_deadline_is_rechecked_at_the_write_boundary():
    """The pre-transaction check is necessary and not sufficient: a process
    can pass it and cross the deadline before the INSERT."""

    import app.forecast_lab.official_slate as module

    season_id = _season("boundary")
    original = module.propose_slate

    def propose_in_time(**kwargs):
        kwargs["now"] = DEADLINE - timedelta(minutes=5)
        proposal = original(**kwargs)
        assert proposal.past_deadline is False, "the proposal was already late"
        return proposal

    module.propose_slate = propose_in_time
    try:
        with pytest.raises(SlateCommitRefused, match="passed between proposal and write"):
            commit_official_slate(
                season_id=season_id, week_number=3,
                schedule_provider=StubSchedule(),
                now=DEADLINE + timedelta(seconds=1),
            )
    finally:
        module.propose_slate = original

    assert _count(BenchmarkSlatePlan) == 0
    assert _count(BenchmarkSlateFixture) == 0
    assert _count(BenchmarkSlot) == 0


def test_two_concurrent_committers_produce_one_plan_and_one_clean_refusal():
    """Not one success plus an unhandled UNIQUE-constraint IntegrityError.
    The Week row is taken FOR UPDATE, so the loser serializes behind it,
    sees the committed plan and refuses."""

    import threading

    season_id = _season("race")
    start = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            start.wait(timeout=10)
            commit_official_slate(
                season_id=season_id, week_number=3,
                schedule_provider=StubSchedule(), now=IN_TIME,
            )
            result = "committed"
        except SlateCommitRefused:
            result = "refused"
        except Exception as exc:  # pragma: no cover - diagnostic
            result = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(outcomes) == ["committed", "refused"], outcomes
    assert _count(BenchmarkSlatePlan) == 1


def test_no_network_happens_inside_the_commit_transaction():
    import ast
    import inspect
    import textwrap

    from app.forecast_lab import official_slate

    source = inspect.getsource(official_slate.commit_official_slate)
    tree = ast.parse(textwrap.dedent(source))
    blocks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and "session_scope" in ast.dump(node.items[0].context_expr)
    ]
    assert blocks, "commit_official_slate no longer opens a session_scope block"
    names: set[str] = set()
    for block in blocks:
        names |= {
            n.func.attr for n in ast.walk(block)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        } | {
            n.func.id for n in ast.walk(block)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
    for forbidden in ("fetch_schedule", "propose_slate", "planned_pool", "record_call"):
        assert forbidden not in names, f"{forbidden} runs inside the commit lock"


def test_the_commit_transaction_locks_the_week_and_the_rules():
    """The concurrency test alone cannot prove this: two Python threads can
    serialize by accident and still pass. The locks are asserted
    structurally as well."""

    import ast
    import inspect
    import textwrap

    from app.forecast_lab import official_slate

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(official_slate.commit_official_slate))
    )
    # The Week row is locked inline, to serialize committers.
    locked = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
    ]
    assert any("Week" in ast.dump(node) for node in locked), "the Week row is not locked"

    # The RULES are RESOLVED under the lock, in one statement. An earlier
    # version resolved the active row unlocked and then locked that id,
    # which proved nothing: if an amendment landed in between, it locked the
    # superseded parent whose rules_version still matched the proposal.
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    resolved_under_lock = [
        node for node in calls
        if node.func.id == "active_rules"
        and any(kw.arg == "lock" and getattr(kw.value, "value", None) is True
                for kw in node.keywords)
    ]
    assert resolved_under_lock, (
        "the commit transaction must resolve the ACTIVE rules row under the "
        "lock via active_rules(..., lock=True), not resolve it unlocked and "
        "then lock the id it already chose"
    )
    assert not any(node.func.id == "_season_pins" for node in calls), (
        "_season_pins does an UNLOCKED active read; using it at the write "
        "boundary reintroduces the stale-parent race"
    )


def test_the_planning_input_fingerprint_is_persisted_and_distinct():
    season_id = _season("planning-fp")
    proposal = _commit(season_id)
    with session_scope() as session:
        plan = session.get(BenchmarkSlatePlan, proposal.plan_id)
        assert plan.planning_input_fingerprint
        assert len(plan.planning_input_fingerprint) == 64
        assert plan.planning_input_fingerprint != plan.fixture_pool_fingerprint, (
            "one fingerprint cannot honestly mean both same-pool and same-input"
        )
        assert plan.resolver_version and plan.fixture_key_version


def test_an_official_plan_without_the_new_provenance_is_refused_by_the_database():
    from sqlalchemy.exc import IntegrityError

    from app.forecast_lab.fixture_identity import FIXTURE_KEY_VERSION

    season_id = _season("new-prov")
    with session_scope() as session:
        week_id = session.execute(
            select(Week.id).where(Week.season_id == season_id)
        ).scalar_one()
    # Everything the OLD check required, and nothing more.
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(BenchmarkSlatePlan(
                week_id=week_id, target_slot_count=5,
                allocation_method="STABLE_HASH_V1", committed_at=IN_TIME,
                is_official=True, rules_version="x",
                schedule_provider_call_id=None,
                fixture_pool_fingerprint="a" * 64, fixture_pool_count=16,
                earliest_opening_at=DEADLINE,
                fixture_key_version=FIXTURE_KEY_VERSION,
                resolver_version="r",
            ))


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
        schedule_provider=StubSchedule(), now=IN_TIME,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "PREVIEW" in out
    assert _count(BenchmarkSlatePlan) == 0


def test_the_cli_preview_shows_the_complete_pool_and_the_slots(capsys):
    season_id = _season("cli-pool")
    main(["--season-id", str(season_id), "--week-number", "3"],
         schedule_provider=StubSchedule(), now=IN_TIME)
    out = capsys.readouterr().out
    for away, home, _ in FIXTURES:
        assert f"2026:REG:W03:{away}@{home}" in out
    assert out.count("<-- SLOT") == PRODUCTION_SLATE_SIZE
    assert "slot 1" in out and "slot 10" in out


def test_the_cli_refuses_cleanly(capsys):
    season_id = _season("cli-refuse", method=None)
    code = main(["--season-id", str(season_id), "--week-number", "3"],
                schedule_provider=StubSchedule(), now=IN_TIME)
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

    assert report.schedule_fixture_count == 16
    assert report.plan_committed is False
    assert report.registered_count == 0
    assert report.earliest_opening_at == DEADLINE
    assert report.past_commit_deadline is False

    text = report.render()
    assert "NOT COMMITTED" in text
    assert "Odds Games registered   0/16" in text
    assert "incomplete (0/16)" in text


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
    assert "incomplete (8/16)" in report.render()


def test_readiness_uses_the_committed_plans_own_frozen_pool():
    """A later schedule release must not silently recompute a committed
    week. The plan froze what the allocator saw."""

    season_id = _season("readiness-committed")
    proposal = _commit(season_id)
    smaller = StubSchedule(games=FIXTURES[:4])

    report = _readiness(season_id, schedule=smaller)
    assert report.plan_committed is True
    assert report.plan_id == proposal.plan_id
    assert report.schedule_fixture_count == 16, "it recomputed from a fresh fetch"
    assert report.plan_fingerprint == proposal.fingerprint
    assert smaller.calls == 0, "it fetched a schedule it did not need"


def test_readiness_names_slotted_fixtures_with_no_registered_game():
    season_id = _season("readiness-coverage")
    _commit(season_id)
    report = _readiness(season_id)

    assert len(report.slotted) == PRODUCTION_SLATE_SIZE
    assert len(report.unbound_slots) == PRODUCTION_SLATE_SIZE
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


# --- the stale-parent race the lock must actually close ----------------


def test_an_amendment_between_proposal_and_lock_cannot_pass_as_current():
    """The precise failure the earlier lock did not close.

    It resolved the active row UNLOCKED, then locked that id. If an
    amendment landed in between, the locked row was the superseded PARENT —
    whose rules_version still equalled the proposal's, so the comparison
    passed and the slate committed under methodology that was no longer
    active. Resolving the active row UNDER the lock makes that impossible.
    """

    import app.forecast_lab.official_slate as module

    season_id = _season("stale-parent")
    original = module.propose_slate

    def supersede_after_proposal(**kwargs):
        proposal = original(**kwargs)
        # A real amendment: the parent is superseded and a NEW active row
        # appears. The parent's own rules_version is untouched, which is
        # exactly why locking it by id proved nothing.
        with session_scope() as session:
            parent = session.execute(
                select(SeasonRules).where(
                    SeasonRules.season_id == season_id,
                    SeasonRules.superseded_by.is_(None),
                )
            ).scalar_one()
            clone = SeasonRules(
                **{
                    c.name: getattr(parent, c.name)
                    for c in SeasonRules.__table__.columns
                    if c.name not in {"id", "created_at"}
                }
            )
            clone.rules_version = f"{parent.rules_version}-v3"
            session.add(clone)
            session.flush()
            parent.superseded_by = clone.id
        return proposal

    module.propose_slate = supersede_after_proposal
    try:
        with pytest.raises(SlateCommitRefused, match="methodology changed"):
            commit_official_slate(
                season_id=season_id, week_number=3,
                schedule_provider=StubSchedule(), now=IN_TIME,
            )
    finally:
        module.propose_slate = original

    assert _count(BenchmarkSlatePlan) == 0
    with session_scope() as session:
        active = session.execute(
            select(SeasonRules).where(
                SeasonRules.season_id == season_id,
                SeasonRules.superseded_by.is_(None),
            )
        ).scalar_one()
        assert active.rules_version.endswith("-v3"), "the fixture did not amend"


# --- committed_at is the write-boundary clock --------------------------


def test_committed_at_is_the_write_boundary_time_not_the_proposal_time():
    """The deadline is enforced against the write clock, so calling the
    earlier proposal moment "committed" would put a timestamp in the
    durable record that no write ever happened at."""

    import app.forecast_lab.official_slate as module

    season_id = _season("committed-at")
    proposed_at = DEADLINE - timedelta(hours=6)
    written_at = DEADLINE - timedelta(minutes=30)
    original = module.propose_slate

    def propose_early(**kwargs):
        kwargs["now"] = proposed_at
        return original(**kwargs)

    module.propose_slate = propose_early
    try:
        proposal = commit_official_slate(
            season_id=season_id, week_number=3,
            schedule_provider=StubSchedule(), now=written_at,
        )
    finally:
        module.propose_slate = original

    assert proposal.decided_at == proposed_at
    assert proposal.committed_at == written_at
    with session_scope() as session:
        plan = session.get(BenchmarkSlatePlan, proposal.plan_id)
        assert plan.committed_at == written_at, (
            "the plan records a time no write happened at"
        )
        assert plan.committed_at != proposed_at


def test_the_planning_input_version_is_persisted():
    from app.forecast_lab.fixture_identity import PLANNING_INPUT_VERSION

    season_id = _season("planning-version")
    proposal = _commit(season_id)
    with session_scope() as session:
        plan = session.get(BenchmarkSlatePlan, proposal.plan_id)
        assert plan.planning_input_version == PLANNING_INPUT_VERSION, (
            "an auditor holding the digest cannot tell which format produced it"
        )


def test_an_official_plan_without_the_planning_version_is_refused_by_the_database():
    from sqlalchemy.exc import IntegrityError

    from app.forecast_lab.fixture_identity import FIXTURE_KEY_VERSION

    season_id = _season("no-planning-version")
    with session_scope() as session:
        week_id = session.execute(
            select(Week.id).where(Week.season_id == season_id)
        ).scalar_one()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(BenchmarkSlatePlan(
                week_id=week_id, target_slot_count=5,
                allocation_method="STABLE_HASH_V1", committed_at=IN_TIME,
                is_official=True, rules_version="x",
                schedule_provider_call_id=None,
                fixture_pool_fingerprint="a" * 64,
                planning_input_fingerprint="b" * 64,
                fixture_pool_count=16, earliest_opening_at=DEADLINE,
                fixture_key_version=FIXTURE_KEY_VERSION, resolver_version="r",
            ))


# --- readiness reports the week lifecycle ------------------------------


def test_readiness_reports_an_absent_week_row():
    season_id = _season("no-week-row", week_number=None)
    report = _readiness(season_id)
    assert report.week_present is False
    text = report.render()
    assert "week row                ABSENT" in text
    assert "NO WEEK ROW" in text
    assert "cannot occur until the week is PREPARED" in text


def test_readiness_reports_a_pending_week_as_committable():
    season_id = _season("pending-week")
    report = _readiness(season_id)
    assert report.week_present is True
    assert report.week_status == "PENDING"
    text = report.render()
    assert "week row                PRESENT" in text
    # All THREE durable flags, not just the money one. A report that showed
    # only is_real_money would read identically for a rehearsal and for a
    # half-rehearsal that still counts toward standings and awards.
    assert "week mode               REHEARSAL" in text
    assert "is_real_money           False" in text
    assert "counts_toward_standings False" in text
    assert "counts_toward_awards    False" in text
    assert "is PENDING REHEARSAL" in text
    assert "A slate may be committed against it" in text


def test_readiness_reports_a_half_rehearsal_week_as_nonstandard():
    """The exact state the old preparation CLI could create: no money on
    it, still counting toward standings and awards. Not a rehearsal with a
    typo -- a combination nobody approved."""

    season_id = _season("nonstandard-week", week_number=None)
    with session_scope() as session:
        session.add(Week(
            season_id=season_id, week_number=3, is_real_money=False,
            counts_toward_standings=True, counts_toward_awards=True,
        ))

    report = _readiness(season_id)
    assert report.week_profile == "NONSTANDARD"
    text = report.render()
    assert "week mode               NONSTANDARD" in text
    assert "half rehearsal and half competitive" in text


def test_readiness_reports_an_opened_week_distinctly():
    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("opened-week", week_number=None)
    SeasonCommissioner(season_id=season_id).open_week(week_number=3, is_real_money=False)
    report = _readiness(season_id)
    assert report.week_status == "OPENED"
    assert report.week_opened_at is not None
    assert "the competition week has begun" in report.render()



def test_the_cli_takes_no_clock_override_from_the_command_line():
    """A deadline an operator can move is not a deadline. The `now` seam is
    keyword-only and no argv flag reaches it."""

    import inspect

    signature = inspect.signature(main)
    assert signature.parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    for flag in ("--now", "--as-of", "--clock", "--force"):
        with pytest.raises(SystemExit):
            main(["--season-id", str(uuid.uuid4()), "--week-number", "3", flag,
                  "2026-01-01T00:00:00Z"], schedule_provider=StubSchedule())
