"""BenchmarkSlatePlan / BenchmarkSlot persistence (DATABASE.md §5)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.forecast_lab import BenchmarkSlatePlan, BenchmarkSlot


class BenchmarkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_plan(self, row: BenchmarkSlatePlan) -> BenchmarkSlatePlan:
        self.session.add(row)
        self.session.flush()
        return row

    def add_slot(self, row: BenchmarkSlot) -> BenchmarkSlot:
        self.session.add(row)
        self.session.flush()
        return row

    def get_plan_for_week(self, week_id: uuid.UUID) -> BenchmarkSlatePlan | None:
        stmt = select(BenchmarkSlatePlan).where(BenchmarkSlatePlan.week_id == week_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def pending_slots_for_game(self, plan_id: uuid.UUID, game_id: uuid.UUID) -> list[BenchmarkSlot]:
        stmt = select(BenchmarkSlot).where(
            BenchmarkSlot.plan_id == plan_id, BenchmarkSlot.game_id == game_id, BenchmarkSlot.status == "PENDING"
        )
        return list(self.session.execute(stmt).scalars())

    def slots_for_plan(self, plan_id: uuid.UUID) -> list[BenchmarkSlot]:
        stmt = select(BenchmarkSlot).where(BenchmarkSlot.plan_id == plan_id).order_by(BenchmarkSlot.slot_index)
        return list(self.session.execute(stmt).scalars())
