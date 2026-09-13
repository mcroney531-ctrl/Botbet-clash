"""Engine/session management. One process-wide engine; sessions are
short-lived and always used as a context manager so a transaction
boundary is always explicit (ARCHITECTURE.md §6's "transaction
boundaries" requirement carries over from Phase 1's in-memory rule)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # Local/test default: matches the botbet_test database provisioned
    # for this environment's Postgres 16 cluster.
    return "postgresql+psycopg://postgres:postgres@localhost:5432/botbet_test"


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """One unit of work. Commits on clean exit, rolls back on any
    exception — this is the mechanism behind every "must commit
    atomically" requirement in ARCHITECTURE.md's Phase 2 transaction
    boundaries: callers do their inserts/updates inside this block and
    never call `session.commit()` themselves."""

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests() -> None:
    """Test-only: drop the cached engine/sessionmaker so a test can point
    DATABASE_URL somewhere else and get a fresh connection pool."""

    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
