"""Preparing a week is not opening it.

`BenchmarkSlatePlan.week_id` must exist BEFORE the first OPENING window,
because the slate is precommitted. The only persistence path that made a
Week row was `open_week`, which creates it already OPENED and emits
`WEEK_OPENED` — so precommitting the research sample required declaring the
competition week open first, purely as a schema side effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.events import CompetitionEvent
from app.db.models.markets import Game
from app.db.models.season import Season, SeasonRules, Week
from app.db.session import session_scope
from app.services.prepare_week import (
    WeekPreparationRefused,
    apply_preparation,
    main,
    plan_preparation,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _season(tag):
    with session_scope() as session:
        season = Season(year=2026, name=f"prep-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=f"prep-{tag}-{uuid.uuid4()}",
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider="THE_ODDS_API", roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards"], devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=5, batch_methodology="SINGLE_BATCH",
            checkpoint_windows={
                "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
                "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
                "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
            },
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        return season.id


def _week(season_id, number=4):
    with session_scope() as session:
        return session.execute(
            select(Week).where(Week.season_id == season_id, Week.week_number == number)
        ).scalar_one_or_none()


def _count(model) -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(model)).scalar()


def test_a_prepared_week_is_pending_and_not_opened():
    season_id = _season("pending")
    apply_preparation(season_id=season_id, week_number=4, is_real_money=False)

    week = _week(season_id)
    assert week is not None
    assert week.status == "PENDING"
    assert week.opened_at is None


def test_preparing_emits_no_week_opened_event():
    """Opening a week is a competitive declaration. Creating the FK target
    a precommitted slate points at is not."""

    season_id = _season("no-event")
    before = _count(CompetitionEvent)
    apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    assert _count(CompetitionEvent) == before


def test_preparing_touches_nothing_else():
    from app.db.models.forecast_lab import BenchmarkSlatePlan
    from app.db.models.settlement import BankrollTransaction

    season_id = _season("narrow")
    before = (_count(Game), _count(BankrollTransaction), _count(BenchmarkSlatePlan))
    apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    assert (_count(Game), _count(BankrollTransaction), _count(BenchmarkSlatePlan)) == before


def test_preparation_is_idempotent():
    season_id = _season("idempotent")
    first = apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    second = apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    assert first.week_id == second.week_id
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1


def test_a_contradictory_real_money_flag_is_refused_not_adjusted():
    """That flag decides whether a week counts toward the season. Changing
    it under an already-committed slate would rewrite what was agreed."""

    season_id = _season("conflict")
    apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    with pytest.raises(WeekPreparationRefused, match="is not adjusted in place"):
        apply_preparation(season_id=season_id, week_number=4, is_real_money=True)
    assert _week(season_id).is_real_money is False


def test_the_repository_refuses_a_conflicting_flag_on_its_own():
    """Defence in depth. The service checks first, so the repository's own
    guard is otherwise never exercised -- and the repository is what any
    future caller reaches."""

    from app.db.repositories.season_repository import (
        SeasonRepository,
        WeekConfigurationConflict,
    )

    season_id = _season("repo-guard")
    with session_scope() as session:
        SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, is_real_money=False
        )
    with pytest.raises(WeekConfigurationConflict, match="not adjusted in place"):
        with session_scope() as session:
            SeasonRepository(session).prepare_week(
                season_id=season_id, week_number=4, is_real_money=True
            )
    assert _week(season_id).is_real_money is False


def test_the_repository_returns_the_same_row_for_an_identical_request():
    from app.db.repositories.season_repository import SeasonRepository

    season_id = _season("repo-idempotent")
    with session_scope() as session:
        first = SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, is_real_money=False
        ).id
    with session_scope() as session:
        second = SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, is_real_money=False
        ).id
    assert first == second


def test_opening_transitions_a_prepared_week_rather_than_creating_one():
    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("transition")
    prepared = apply_preparation(season_id=season_id, week_number=4, is_real_money=False)

    opened_id = SeasonCommissioner(season_id=season_id).open_week(
        week_number=4, is_real_money=False
    )
    assert uuid.UUID(opened_id) == prepared.week_id, "opening created a second row"

    week = _week(season_id)
    assert week.status == "OPENED"
    assert week.opened_at is not None
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1


def test_opening_without_preparing_first_still_works():
    """The old single-call ergonomics survive: existing callers are
    unchanged while production gains the ability to prepare separately."""

    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("legacy")
    SeasonCommissioner(season_id=season_id).open_week(week_number=4, is_real_money=False)
    week = _week(season_id)
    assert week.status == "OPENED"
    assert week.opened_at is not None


def test_the_dry_run_writes_nothing(capsys):
    season_id = _season("dry")
    code = main([
        "--season-id", str(season_id), "--week-number", "4", "--no-real-money",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert "WOULD CREATE" in out
    assert "does NOT open the competition" in out
    assert _week(season_id) is None


def test_the_dry_run_reports_an_existing_week(capsys):
    season_id = _season("dry-existing")
    apply_preparation(season_id=season_id, week_number=4, is_real_money=False)
    main(["--season-id", str(season_id), "--week-number", "4", "--no-real-money"])
    out = capsys.readouterr().out
    assert "EXISTS ALREADY" in out
    assert "PENDING" in out


def test_week_zero_is_refused():
    season_id = _season("week-zero")
    with pytest.raises(WeekPreparationRefused, match="real NFL week"):
        plan_preparation(season_id=season_id, week_number=0, is_real_money=False)


def test_the_cli_refuses_cleanly(capsys):
    code = main([
        "--season-id", str(uuid.uuid4()), "--week-number", "4", "--no-real-money",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    assert "Traceback" not in out


def test_preparation_reaches_no_provider():
    import ast
    import inspect

    from app.services import prepare_week

    tree = ast.parse(inspect.getsource(prepare_week))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("fetch_schedule", "list_events", "fetch_quotes",
                      "TheOddsApiProvider", "commit_official_slate"):
        assert forbidden not in names, f"week preparation reaches {forbidden}"
