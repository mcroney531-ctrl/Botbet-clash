"""Ticket / Wager / PassDecision / Settlement persistence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.competition import PassDecision as PassDecisionRow
from app.db.models.competition import Ticket as TicketRow
from app.db.models.competition import Wager as WagerRow
from app.db.models.settlement import Settlement as SettlementRow


class CompetitionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- tickets ---------------------------------------------------------

    def add_ticket(self, row: TicketRow) -> TicketRow:
        self.session.add(row)
        self.session.flush()
        return row

    def get_ticket(self, ticket_id: uuid.UUID) -> TicketRow:
        row = self.session.get(TicketRow, ticket_id)
        if row is None:
            raise LookupError(f"ticket {ticket_id} not found")
        return row

    def active_pounce_tickets(self, season_competitor_id: uuid.UUID, week_id: uuid.UUID, now: datetime) -> list[TicketRow]:
        """`status == ISSUED` alone isn't "active" - an expired,
        unexecuted Pounce may be replaced (RULES.md §74), so a ticket
        past its own `valid_until` must not count against the limit even
        though nothing has flipped its `status` to EXPIRED yet."""

        stmt = select(TicketRow).where(
            TicketRow.season_competitor_id == season_competitor_id,
            TicketRow.week_id == week_id,
            TicketRow.urgency == "POUNCE",
            TicketRow.status == "ISSUED",
            or_(TicketRow.valid_until.is_(None), TicketRow.valid_until > now),
        )
        return list(self.session.execute(stmt).scalars())

    # -- wagers ---------------------------------------------------------

    def add_wager(self, row: WagerRow) -> WagerRow:
        self.session.add(row)
        self.session.flush()
        return row

    def get_wager(self, wager_id: uuid.UUID) -> WagerRow:
        row = self.session.get(WagerRow, wager_id)
        if row is None:
            raise LookupError(f"wager {wager_id} not found")
        return row

    def get_wager_by_ticket(self, ticket_id: uuid.UUID) -> WagerRow | None:
        stmt = select(WagerRow).where(WagerRow.ticket_id == ticket_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def has_placed_wager(self, season_competitor_id: uuid.UUID, week_id: uuid.UUID) -> bool:
        stmt = select(WagerRow.id).where(
            WagerRow.season_competitor_id == season_competitor_id,
            WagerRow.week_id == week_id,
            WagerRow.execution_status == "PLACED",
        )
        return self.session.execute(stmt).first() is not None

    # -- pass decisions ---------------------------------------------------

    def add_pass_decision(self, row: PassDecisionRow) -> PassDecisionRow:
        self.session.add(row)
        self.session.flush()
        return row

    def has_pass_decision(self, season_competitor_id: uuid.UUID, week_id: uuid.UUID) -> bool:
        stmt = select(PassDecisionRow.id).where(
            PassDecisionRow.season_competitor_id == season_competitor_id,
            PassDecisionRow.week_id == week_id,
        )
        return self.session.execute(stmt).first() is not None

    # -- settlements ---------------------------------------------------

    def add_settlement(self, row: SettlementRow) -> SettlementRow:
        self.session.add(row)
        self.session.flush()
        return row

    def get_settlement_by_wager(self, wager_id: uuid.UUID) -> SettlementRow | None:
        stmt = select(SettlementRow).where(SettlementRow.wager_id == wager_id)
        return self.session.execute(stmt).scalar_one_or_none()
