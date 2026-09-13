"""Bankroll ledger persistence. Current bankroll is never stored — always
derived by SUM(amount_cents), scoped by season_competitor_id so it can
never blend two seasons' money (DATABASE.md §8)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models.settlement import BankrollTransaction as BankrollTransactionRow


class LedgerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, row: BankrollTransactionRow) -> BankrollTransactionRow:
        self.session.add(row)
        self.session.flush()
        return row

    def available_balance(self, season_competitor_id: uuid.UUID) -> Money:
        stmt = select(func.coalesce(func.sum(BankrollTransactionRow.amount_cents), 0)).where(
            BankrollTransactionRow.season_competitor_id == season_competitor_id
        )
        total = self.session.execute(stmt).scalar_one()
        return Money(int(total))

    def transactions_for(self, season_competitor_id: uuid.UUID) -> list[BankrollTransactionRow]:
        stmt = (
            select(BankrollTransactionRow)
            .where(BankrollTransactionRow.season_competitor_id == season_competitor_id)
            .order_by(BankrollTransactionRow.created_at)
        )
        return list(self.session.execute(stmt).scalars())
