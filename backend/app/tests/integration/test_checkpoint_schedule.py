"""The read-only checkpoint schedule projector.

Two questions kept being answered with "if the windows are 144/96, then
roughly..." -- a guess about production dressed as arithmetic. These tests
pin the properties that make the projection trustworthy: it reads the
FROZEN windows, it uses the SAME window arithmetic the capture cycle uses,
it counts unregistered fixtures toward the slate deadline, and it spends
nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.markets import CheckpointRun, Game
from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.forecast_lab.checkpoint_window import CheckpointDisposition, compute_window
from app.marketdata.checkpoint_schedule import (
    ScheduleProjectionFailed,
    main,
    project_week,
    render,
)
from app.rosterdata.teams import CanonicalTeam
from app.scheduledata.base import (
    ScheduledGame,
    ScheduleDataError,
    ScheduleFetchResult,
    ScheduleSnapshot,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 9, 25, 0, 15, tzinfo=timezone.utc)
WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}


class StubSchedule:
    provider_name = "NFLVERSE"

    def __init__(self, games=(), *, ok=True):
        self.games = tuple(games)
        self.ok = ok
        self.calls = 0

    def fetch_schedule(self, *, season):
        self.calls += 1
        if not self.ok:
            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(category="ROSTER_SOURCE_UNAVAILABLE", message="down"),
                call_metadata=None,
            )
        return ScheduleFetchResult(
            payload=ScheduleSnapshot(
                provider="NFLVERSE", season=season, retrieved_at=NOW, games=self.games,
            ),
            error=None, call_metadata=None,
        )


def _scheduled(away, home, week, kickoff):
    return ScheduledGame(
        season=2026, week=week, game_type="REG",
        home=CanonicalTeam(home), away=CanonicalTeam(away), kickoff_at=kickoff,
    )


def _season(tag, *, windows=None):
    with session_scope() as session:
        season = Season(year=2026, name=f"proj-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=f"proj-{tag}-{uuid.uuid4()}",
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider="THE_ODDS_API", roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards"], devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=5, batch_methodology="SINGLE_BATCH",
            checkpoint_windows=WINDOWS if windows is None else windows,
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        return season.id


def _game(season_id, away, home, *, week=3, kickoff=KICKOFF):
    with session_scope() as session:
        game = Game(
            external_ref=f"THE_ODDS_API:{uuid.uuid4()}", season_id=season_id,
            week_number=week, home_team=home, away_team=away,
            home_team_canonical=home, away_team_canonical=away, kickoff_at=kickoff,
        )
        session.add(game)
        session.flush()
        return game.id


def test_the_projection_reads_the_frozen_windows_not_a_default():
    """The whole reason this exists: 144/96 must come from the season's
    rules, not from anyone's memory of them."""

    odd = {
        "OPENING": {"start_hours_before_kickoff": 100, "end_hours_before_kickoff": 90},
        "MID": {"start_hours_before_kickoff": 50, "end_hours_before_kickoff": 30},
        "FINAL": {"start_hours_before_kickoff": 5, "end_hours_before_kickoff": 1},
    }
    season_id = _season("frozen", windows=odd)
    _game(season_id, "ATL", "GB")

    report = project_week(
        season_id=season_id, week_number=3, schedule_provider=StubSchedule(), now=NOW,
    )
    window = report.fixtures[0].windows["OPENING"]
    assert window.window_start == KICKOFF - timedelta(hours=100)
    assert window.window_end == KICKOFF - timedelta(hours=90)


def test_the_projection_uses_the_same_arithmetic_as_the_capture_cycle():
    """A second copy of this maths could disagree with the cycle it is
    supposed to be predicting, and a projection you cannot trust is worse
    than no projection."""

    season_id = _season("same-maths")
    _game(season_id, "ATL", "GB")

    report = project_week(
        season_id=season_id, week_number=3, schedule_provider=StubSchedule(), now=NOW,
    )
    for name in ("OPENING", "MID", "FINAL"):
        assert report.fixtures[0].windows[name] == compute_window(KICKOFF, name, WINDOWS)


def test_an_unregistered_fixture_still_counts_toward_the_slate_deadline():
    """A deadline computed only from REGISTERED games moves later every
    time the provider is slow to list one -- precisely backwards, since the
    unlisted game is the reason to hurry."""

    season_id = _season("deadline")
    late = KICKOFF + timedelta(days=2)
    early = KICKOFF - timedelta(days=1)
    _game(season_id, "ATL", "GB", kickoff=late)

    report = project_week(
        season_id=season_id, week_number=3, now=NOW,
        schedule_provider=StubSchedule([
            _scheduled("ATL", "GB", 3, late),
            _scheduled("NYJ", "DET", 3, early),   # real, not registered
        ]),
    )

    assert len(report.unregistered) == 1
    assert report.unregistered[0].label == "NYJ @ DET"
    assert report.slate_deadline() == early - timedelta(hours=144), (
        "the deadline ignored a fixture that exists but is not registered"
    )


def test_a_registered_fixture_is_not_duplicated_by_the_schedule():
    season_id = _season("dedupe")
    _game(season_id, "ATL", "GB")

    report = project_week(
        season_id=season_id, week_number=3, now=NOW,
        schedule_provider=StubSchedule([_scheduled("ATL", "GB", 3, KICKOFF)]),
    )
    assert len(report.fixtures) == 1
    assert report.fixtures[0].registered is True


def test_only_the_requested_week_is_projected():
    season_id = _season("one-week")
    _game(season_id, "ATL", "GB")

    report = project_week(
        season_id=season_id, week_number=3, now=NOW,
        schedule_provider=StubSchedule([
            _scheduled("ATL", "GB", 3, KICKOFF),
            _scheduled("KC", "MIA", 4, KICKOFF + timedelta(days=7)),
        ]),
    )
    assert [f.label for f in report.fixtures] == ["ATL @ GB"]


def test_an_existing_captured_run_is_reported_as_already_captured():
    season_id = _season("captured")
    game_id = _game(season_id, "ATL", "GB")
    with session_scope() as session:
        session.add(CheckpointRun(
            game_id=game_id, checkpoint_type="OPENING",
            window_start=KICKOFF - timedelta(hours=144),
            window_end=KICKOFF - timedelta(hours=96),
            target_time=KICKOFF - timedelta(hours=144),
            status="CAPTURED", captured_at=NOW,
        ))

    report = project_week(
        season_id=season_id, week_number=3, schedule_provider=StubSchedule(), now=NOW,
    )
    assert report.fixtures[0].dispositions["OPENING"] is CheckpointDisposition.ALREADY_CAPTURED


def test_open_now_and_next_to_open_are_computed_from_the_clock():
    season_id = _season("clock")
    _game(season_id, "ATL", "GB")
    stub = StubSchedule()

    # OPENING for a KICKOFF-144h..-96h window runs 2026-09-19 .. 2026-09-21.
    during = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    report = project_week(
        season_id=season_id, week_number=3, schedule_provider=stub, now=during,
    )
    assert [name for _, name, _ in report.open_now()] == ["OPENING"]

    before = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    report = project_week(
        season_id=season_id, week_number=3, schedule_provider=stub, now=before,
    )
    assert report.open_now() == []
    fixture, name, window = report.next_window_opening()
    assert name == "OPENING"
    assert window.window_start == KICKOFF - timedelta(hours=144)


def test_a_missing_frozen_policy_refuses_rather_than_guessing():
    season_id = _season("empty", windows={})
    with pytest.raises(ScheduleProjectionFailed, match="no .*checkpoint_windows"):
        project_week(
            season_id=season_id, week_number=3, schedule_provider=StubSchedule(), now=NOW,
        )


def test_a_schedule_failure_is_reported_not_silently_ignored():
    """Silently projecting only the registered games would understate the
    deadline in exactly the situation where it matters."""

    season_id = _season("sched-down")
    _game(season_id, "ATL", "GB")

    report = project_week(
        season_id=season_id, week_number=3, now=NOW,
        schedule_provider=StubSchedule(ok=False),
    )
    assert report.schedule_note is not None
    assert "NOT accounted for" in report.schedule_note
    assert "NOTE" in render(report)


def test_the_projector_writes_nothing_and_calls_no_odds_provider():
    import ast
    import inspect

    from app.marketdata import checkpoint_schedule

    season_id = _season("readonly")
    _game(season_id, "ATL", "GB")
    before = {}
    with session_scope() as session:
        for model in (Game, CheckpointRun):
            before[model] = session.execute(
                select(func.count()).select_from(model)
            ).scalar()

    project_week(
        season_id=season_id, week_number=3, schedule_provider=StubSchedule(), now=NOW,
    )

    with session_scope() as session:
        for model, count in before.items():
            assert session.execute(
                select(func.count()).select_from(model)
            ).scalar() == count, f"the projector wrote {model.__name__} rows"

    tree = ast.parse(inspect.getsource(checkpoint_schedule))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("fetch_quotes", "list_events", "TheOddsApiProvider",
                      "capture_checkpoint", "run_checkpoint_cycle"):
        assert forbidden not in names, f"the projector reaches {forbidden}"


def test_the_cli_exits_cleanly_when_it_cannot_project(capsys):
    season_id = _season("cli-empty", windows={})
    code = main(
        ["--season-id", str(season_id), "--week-number", "3"],
        schedule_provider=StubSchedule(),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "CANNOT PROJECT" in out
    assert "Traceback" not in out


def test_the_report_names_the_unregistered_fixtures_and_the_deadline():
    season_id = _season("render")
    _game(season_id, "ATL", "GB")
    report = project_week(
        season_id=season_id, week_number=3, now=NOW,
        schedule_provider=StubSchedule([
            _scheduled("ATL", "GB", 3, KICKOFF),
            _scheduled("NYJ", "DET", 3, KICKOFF + timedelta(days=2)),
        ]),
    )
    text = render(report)
    assert "NYJ @ DET" in text
    assert "NOT REGISTERED" in text
    assert "earliest OPENING window starts" in text
    assert report.rules_version in text
