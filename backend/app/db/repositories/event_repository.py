"""Append-only CompetitionEvent persistence (DATABASE.md §11)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.events import CompetitionEvent as CompetitionEventRow


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, row: CompetitionEventRow) -> CompetitionEventRow:
        self.session.add(row)
        self.session.flush()
        return row

    def list_for_week(self, week_id: uuid.UUID) -> list[CompetitionEventRow]:
        stmt = select(CompetitionEventRow).where(CompetitionEventRow.week_id == week_id).order_by(CompetitionEventRow.timestamp)
        return list(self.session.execute(stmt).scalars())

    def list_for_competitor(self, season_competitor_id: uuid.UUID) -> list[CompetitionEventRow]:
        stmt = (
            select(CompetitionEventRow)
            .where(CompetitionEventRow.season_competitor_id == season_competitor_id)
            .order_by(CompetitionEventRow.timestamp)
        )
        return list(self.session.execute(stmt).scalars())
