"""Real-Postgres integration test fixtures (per the Phase 2 review: don't
trust SQLite to emulate JSONB/UUID/constraints/transaction semantics).

Requires a running Postgres reachable at DATABASE_URL (defaults to the
botbet_test database provisioned in this environment). Tables are
created once per test session and truncated before every test for
isolation — a real commit/rollback per `SeasonCommissioner` call is
exactly the behavior these tests are supposed to exercise, so tests share
the engine rather than wrapping everything in one rolled-back
transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import models  # noqa: F401 -- registers all mapped classes
from app.db.base import Base
from app.db.session import get_engine


def pytest_collection_modifyitems(items):
    # A conftest.py hook applies to the whole session, not just its own
    # directory, so filter explicitly: only items actually collected from
    # under this integration/ directory need Postgres.
    here = str(Path(__file__).parent)
    marker = pytest.mark.integration
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(marker)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Build the test schema BY RUNNING THE MIGRATIONS, not create_all.

    `Base.metadata.create_all` builds tables and constraints and nothing
    else. Everything a migration installs imperatively -- the rehearsal
    money triggers from a3f81c6b57e9, the week-profile freeze from
    b7c249e0f3a1 -- is simply absent. Those triggers were installed by
    hand while they were being written, which meant the database-backstop
    tests passed on one machine and would have failed on a fresh checkout,
    and the schema under test was not the schema in production.

    Running the real chain costs a few seconds once per session and makes
    the two identical, which is the only version of this worth trusting.
    """

    engine = get_engine()
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    with engine.begin() as conn:
        # From zero every session: a database left at an older revision by
        # a previous checkout would otherwise be upgraded from whatever it
        # happened to hold, which is not a state production ever has.
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_db(_create_schema):
    engine = get_engine()
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
    yield
