"""ForecastObservation persistence — append-only (DATABASE.md §5)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.forecast_lab import ForecastObservation


class ForecastRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, row: ForecastObservation) -> ForecastObservation:
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, observation_id: uuid.UUID) -> ForecastObservation:
        row = self.session.get(ForecastObservation, observation_id)
        if row is None:
            raise LookupError(f"forecast_observation {observation_id} not found")
        return row

    def for_market(self, market_id: uuid.UUID) -> list[ForecastObservation]:
        stmt = select(ForecastObservation).where(ForecastObservation.market_id == market_id).order_by(ForecastObservation.timestamp)
        return list(self.session.execute(stmt).scalars())

    def latest_for_competitor_market_checkpoint(
        self, season_competitor_id: uuid.UUID, market_id: uuid.UUID, checkpoint_type: str | None
    ) -> ForecastObservation | None:
        """The current (most recent, i.e. tip-of-revision-chain) forecast
        for one competitor/market/checkpoint. Revisions are new rows, so
        "current" just means "latest by timestamp" among rows sharing the
        same (competitor, market, checkpoint) — never an UPDATE target."""

        stmt = (
            select(ForecastObservation)
            .where(
                ForecastObservation.season_competitor_id == season_competitor_id,
                ForecastObservation.market_id == market_id,
                ForecastObservation.checkpoint_type == checkpoint_type,
            )
            .order_by(ForecastObservation.timestamp.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def for_market_and_checkpoint(self, market_id: uuid.UUID, checkpoint_type: str | None) -> list[ForecastObservation]:
        stmt = select(ForecastObservation).where(
            ForecastObservation.market_id == market_id, ForecastObservation.checkpoint_type == checkpoint_type
        )
        return list(self.session.execute(stmt).scalars())
