"""Competitor identity + season-scoped competitor persistence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.season import Competitor as CompetitorRow
from app.db.models.season import SeasonCompetitor as SeasonCompetitorRow


class CompetitorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_identity(self, *, competitor_id: str, provider: str, display_name: str) -> CompetitorRow:
        row = self.session.get(CompetitorRow, competitor_id)
        if row is None:
            row = CompetitorRow(id=competitor_id, provider=provider, display_name=display_name)
            self.session.add(row)
            self.session.flush()
        return row

    def register_season_competitor(
        self,
        *,
        season_id: uuid.UUID,
        competitor_id: str,
        model_identifier: str,
        model_version: str,
        frozen_at: datetime | None = None,
    ) -> SeasonCompetitorRow:
        row = SeasonCompetitorRow(
            season_id=season_id,
            competitor_id=competitor_id,
            model_identifier=model_identifier,
            model_version=model_version,
            status="ACTIVE",
            frozen_at=frozen_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, season_competitor_id: uuid.UUID) -> SeasonCompetitorRow:
        row = self.session.get(SeasonCompetitorRow, season_competitor_id)
        if row is None:
            raise LookupError(f"season_competitor {season_competitor_id} not found")
        return row

    def get_identity(self, competitor_id: str) -> CompetitorRow:
        row = self.session.get(CompetitorRow, competitor_id)
        if row is None:
            raise LookupError(f"competitor {competitor_id} not found")
        return row

    def set_status(self, season_competitor_id: uuid.UUID, status: str) -> None:
        row = self.get(season_competitor_id)
        row.status = status
        self.session.flush()
