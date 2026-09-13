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
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _clean_db(_create_schema):
    engine = get_engine()
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
    yield
