"""DATABASE.md §11 — append-only event stream, drives Show + audit."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid_pk


class CompetitionEvent(Base):
    __tablename__ = "competition_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    timestamp: Mapped[datetime] = mapped_column(nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    week_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("weeks.id"), nullable=True)
    season_competitor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("season_competitors.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_competition_events_season_timestamp", "season_id", "timestamp"),
        Index("ix_competition_events_week_timestamp", "week_id", "timestamp"),
        Index("ix_competition_events_competitor_timestamp", "season_competitor_id", "timestamp"),
    )
