"""The targeted legacy week-profile repair, c5d83b1e7a42.

Four pre-profile smoke weeks carry `is_real_money=False` with standings and
awards left at their `True` column default -- a combination nobody
approved, and the reason migration b7c249e0f3a1 refused to install itself
in production.

The repair's real verification is against databases seeded into the exact
production shape, which is done outside the suite because it needs several
databases at different revisions. What belongs HERE is the part that rots:
the constants. An id quietly edited, a widened precondition or a guard
list that grew to include the research artifacts would all pass every
behavioural test and change what the migration does to production.
"""

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import select, text

from app.db.models.season import Week as WeekRow
from app.db.session import session_scope


def _migration():
    path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "alembic" / "versions" / "c5d83b1e7a42_legacy_week_profile_repair.py"
    )
    spec = importlib.util.spec_from_file_location("_legacy_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repair_names_exactly_the_four_reviewed_rows():
    m = _migration()

    assert len(m.APPROVED_WEEK_IDS) == 4
    assert len(set(m.APPROVED_WEEK_IDS)) == 4, "an id is repeated"
    assert set(m.APPROVED_WEEK_IDS) == {
        "e3e831a4-5001-40ac-8a5d-1aaa21564626",
        "5a51ac4e-49cd-4496-abc2-3716a716914a",
        "37ed5a76-aa38-458d-9365-d54666142181",
        "771fdca8-1fd5-4503-8aaa-25bb1ab2b61c",
    }
    for value in m.APPROVED_WEEK_IDS:
        uuid.UUID(value)  # not a typo'd string that would silently match nothing


def test_the_repair_moves_two_flags_and_leaves_the_money_flag_alone():
    m = _migration()

    assert m.EXPECTED_BEFORE == (False, True, True)
    assert m.REPAIRED_TO == (False, False, False)
    # is_real_money is what these rows ALREADY assert. The repair brings the
    # other two into agreement with it; it does not decide anything new
    # about money.
    assert m.EXPECTED_BEFORE[0] == m.REPAIRED_TO[0] is False


def test_the_repair_never_touches_the_durable_research_season():
    m = _migration()

    assert m.DURABLE_SEASON_ID == "27c34e5b-a6bb-4c05-b0ca-29aba4690808"
    assert "_abort" in _source(m, "upgrade")
    assert "DURABLE_SEASON_ID" in _source(m, "upgrade"), (
        "the durable-season check disappeared from the preflight"
    )


def test_research_artifacts_are_not_grounds_to_refuse_the_repair():
    """Two of the four weeks hold forecasts and all four hold a Game.

    Those are the smoke runs' research output. They are unaffected by the
    two flags being corrected, so requiring them to be absent would refuse
    the repair for a reason that has nothing to do with the repair.
    """

    m = _migration()

    assert set(m.MUST_BE_ZERO) == {
        "tickets", "wagers", "passes", "settlements", "money_rows", "plans",
    }
    for research in ("games", "forecasts", "events"):
        assert research not in m.MUST_BE_ZERO


def test_the_preflight_reads_and_the_update_rechecks_every_precondition():
    """The UPDATE repeats the flag preconditions in its own WHERE clause.

    A preflight that only READS leaves a window between the check and the
    write. Repeating the conditions means a row that changed in between is
    simply not matched, the count comes up short, and the whole
    transaction aborts.
    """

    m = _migration()
    update = _source(m, "upgrade")

    assert "is_real_money = false" in update
    assert "counts_toward_standings = true" in update
    assert "counts_toward_awards = true" in update
    assert "result.rowcount != len(APPROVED_WEEK_IDS)" in update


def test_the_downgrade_is_scoped_to_the_same_four_ids():
    m = _migration()
    down = _source(m, "downgrade")

    assert "id = ANY(:ids)" in down, "downgrade is not scoped by id"
    assert "APPROVED_WEEK_IDS" in down
    # The dangerous shape is a predicate that selects rows by their FLAGS
    # without also pinning the ids -- that is the blanket update this whole
    # approach exists to avoid.
    id_clause = down.index("id = ANY(:ids)")
    where_clause = down.index("WHERE")
    assert where_clause < id_clause, "the id restriction is not in the WHERE clause"


def test_a_database_without_those_rows_is_left_completely_alone():
    """This suite's own schema is built by running the migration chain, so
    the no-op path has already executed by the time this runs. Assert what
    it must have left behind: nothing."""

    with session_scope() as session:
        present = session.execute(
            select(WeekRow.id).where(
                WeekRow.id.in_([uuid.UUID(v) for v in _migration().APPROVED_WEEK_IDS])
            )
        ).scalars().all()
    assert present == []

    with session_scope() as session:
        applied = session.execute(text("select version_num from alembic_version")).scalar()
    assert applied is not None, "the test schema is not migration-built"


def _source(module, function_name: str) -> str:
    import inspect

    return inspect.getsource(getattr(module, function_name))
