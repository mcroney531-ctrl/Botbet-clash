"""Bankroll ledger persistence. Current bankroll is never stored — always
derived by SUM(amount_cents), scoped by season_competitor_id so it can
never blend two seasons' money (DATABASE.md §8)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models.season import Week as WeekRow
from app.db.models.settlement import BankrollTransaction as BankrollTransactionRow
from app.domain.rehearsal import RehearsalBoundaryViolation, alters_official_bankroll
from app.domain.week_profile import WeekFlags, WeekProfile, profile_of


class LedgerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, row: BankrollTransactionRow) -> BankrollTransactionRow:
        """Persist a bankroll movement. REFUSES one scoped to a rehearsal.

        Defence in depth, at the boundary where dollars actually move. The
        Commissioner already branches on the week profile, but this method
        is itself a financial mutation point: anything holding a session can
        call it, and a future service that forgot the profile check would
        move real money on a rehearsal week with nothing in the way.

        `week_id IS NULL` is untouched -- season-start funding is not
        week-scoped and must keep working.
        """

        if row.week_id is not None and alters_official_bankroll(row.type):
            week = self.session.get(WeekRow, row.week_id)
            if week is None:
                raise RehearsalBoundaryViolation(
                    f"bankroll transaction references week {row.week_id}, which "
                    "does not exist; refusing to move money against it"
                )
            profile = profile_of(
                WeekFlags(
                    is_real_money=week.is_real_money,
                    counts_toward_standings=week.counts_toward_standings,
                    counts_toward_awards=week.counts_toward_awards,
                )
            )
            if profile is not WeekProfile.COMPETITIVE:
                raise RehearsalBoundaryViolation(
                    f"refusing a {row.type} of {row.amount_cents} cents against "
                    f"week {row.week_id}, which is "
                    f"{profile or 'NONSTANDARD'}. Official bankroll only moves "
                    "on a COMPETITIVE week."
                )

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
