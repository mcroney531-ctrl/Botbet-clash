"""Immutable EvidenceSnapshot creation (RULES.md §26). For Phase 2 the
payload is caller-supplied (mocked stats/injuries/news/weather/team
context) — reproducibility, not data richness, is what this proves."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.forecast_lab import EvidenceSnapshot


def create_evidence_snapshot(
    session: Session,
    *,
    market_id: uuid.UUID,
    market_snapshot_id: uuid.UUID,
    generated_at: datetime,
    payload: dict,
    checkpoint_run_id: uuid.UUID | None = None,
    checkpoint_type: str | None = None,
) -> EvidenceSnapshot:
    row = EvidenceSnapshot(
        market_id=market_id,
        checkpoint_run_id=checkpoint_run_id,
        checkpoint_type=checkpoint_type,
        generated_at=generated_at,
        payload=payload,
        market_snapshot_id=market_snapshot_id,
    )
    session.add(row)
    session.flush()
    return row


def mock_evidence_payload(*, generated_at: datetime, market_snapshot, extra: dict | None = None) -> dict:
    """A Phase-2-fixture evidence payload — plausible shape, mocked
    content. Real provider integration (Phase 6) fills these fields for
    real instead of changing the shape."""

    payload = {
        "generated_at": generated_at.isoformat(),
        "game": {},
        "player": {},
        "recent_stats": [],
        "team_context": {},
        "injuries": [],
        "news_events": [],
        "weather": {},
        "canonical_market": {
            "line": str(market_snapshot.canonical_line) if market_snapshot.canonical_line is not None else None,
            "over_price": market_snapshot.canonical_over_price,
            "under_price": market_snapshot.canonical_under_price,
        },
        "market_context": {
            "market_median_line": str(market_snapshot.market_median_line) if market_snapshot.market_median_line is not None else None,
            "number_of_books": market_snapshot.number_of_books,
        },
        "line_history": [],
    }
    if extra:
        payload.update(extra)
    return payload
