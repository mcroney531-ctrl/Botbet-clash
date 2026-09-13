"""ResearchSettlement persistence (DATABASE.md §8)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.settlement import ResearchSettlement


class ResearchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, row: ResearchSettlement) -> ResearchSettlement:
        self.session.add(row)
        self.session.flush()
        return row

    def get_for_market(self, market_id: uuid.UUID) -> ResearchSettlement | None:
        stmt = select(ResearchSettlement).where(ResearchSettlement.market_id == market_id)
        return self.session.execute(stmt).scalar_one_or_none()
