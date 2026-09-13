"""DATABASE.md §8 — settlement and ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk


class Settlement(Base):
    """Sportsbook-side settlement of a wager. `wager_id` UNIQUE is the
    hard blocker against double-crediting a wager — the same invariant
    Phase 1's in-memory `DuplicateSettlement` check enforces, now backed
    by a real constraint rather than only application logic."""

    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = uuid_pk()
    wager_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wagers.id"), unique=True, nullable=False)
    sportsbook_result: Mapped[str] = mapped_column(String, nullable=False)
    sportsbook_payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sportsbook_settled_at: Mapped[datetime] = mapped_column(nullable=False)


class ResearchSettlement(Base):
    """Forecast Lab-side settlement of a MARKET's final stat value — never
    a stored OVER/UNDER outcome (DATABASE.md §8's v2 fix). Outcome is
    derived per-`ForecastObservation` against that observation's own
    `canonical_line` at scoring time."""

    __tablename__ = "research_settlements"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), unique=True, nullable=False)
    research_stat_value_at_lock: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    research_locked_at: Mapped[datetime] = mapped_column(nullable=False)
    later_corrected_stat: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    correction_timestamp: Mapped[datetime | None] = mapped_column(nullable=True)


class BankrollTransaction(Base):
    """Append-only. Current bankroll is never stored — always
    SUM(amount_cents) over these, scoped by season_competitor_id so it
    can never blend two seasons (DATABASE.md §1/§8's v2 fix)."""

    __tablename__ = "bankroll_transactions"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    week_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("weeks.id"), nullable=True)
    wager_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wagers.id"), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
