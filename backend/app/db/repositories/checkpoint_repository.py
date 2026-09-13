"""CheckpointRun persistence (DATABASE.md §4)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.markets import CheckpointRun


class CheckpointRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, game_id: uuid.UUID, checkpoint_type: str) -> CheckpointRun | None:
        stmt = select(CheckpointRun).where(CheckpointRun.game_id == game_id, CheckpointRun.checkpoint_type == checkpoint_type)
        return self.session.execute(stmt).scalar_one_or_none()

    def add(self, row: CheckpointRun) -> CheckpointRun:
        self.session.add(row)
        self.session.flush()
        return row
