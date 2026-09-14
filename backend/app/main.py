"""HTTP entrypoint. Deliberately minimal for Phase 3 -- a health surface
so a deploy target (Railway) has an actual process to run and a way to
confirm its DATABASE_URL/credentials are wired correctly, not a full
control surface over the Competition/Forecast Lab/AI layers. Those stay
Python-API-only until a later phase actually needs HTTP access to them.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from app.db.session import get_engine

app = FastAPI(title="BotBet Clash Backend")


@app.get("/health")
def health() -> dict:
    """Liveness only -- no DB dependency, so this stays fast and answers
    even if the database is briefly unreachable."""

    return {"status": "ok"}


@app.get("/health/db")
def health_db() -> dict:
    """Readiness: proves DATABASE_URL actually resolves to a reachable
    Postgres from wherever this process is running -- the one thing that
    took the most manual round-tripping to verify by hand tonight."""

    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}
