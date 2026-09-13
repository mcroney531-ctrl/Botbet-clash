"""DATABASE.md §7 — risk, tickets, wagers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk


class StakeRecommendation(Base):
    __tablename__ = "stake_recommendations"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    forecast_observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecast_observations.id"), nullable=False)
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    bankroll_at_decision_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_probability_over: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    canonical_market_probability_over: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    estimated_edge: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    kelly_fraction_used: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    kelly_reference_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_requested_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    risk_posture: Mapped[str] = mapped_column(String, nullable=False)
    final_allowed_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    week_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weeks.id"), nullable=False)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    stake_recommendation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stake_recommendations.id"), nullable=True)
    urgency: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    risk_posture: Mapped[str] = mapped_column(String, nullable=False)
    kelly_reference_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_requested_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    final_allowed_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_line: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    observed_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("market_snapshots.id"), nullable=True)
    acceptable_line_boundary: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    worst_acceptable_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    why_now: Mapped[str] = mapped_column(String, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ISSUED")
    created_at: Mapped[datetime] = created_at_column()


class Wager(Base):
    __tablename__ = "wagers"

    id: Mapped[uuid.UUID] = uuid_pk()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), unique=True, nullable=False)
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    week_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weeks.id"), nullable=False)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    requested_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_status: Mapped[str] = mapped_column(String, nullable=False)
    sportsbook: Mapped[str | None] = mapped_column(String, nullable=True)
    actual_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    actual_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_stake_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_timestamp: Mapped[datetime | None] = mapped_column(nullable=True)
    bankroll_at_execution_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class PassDecision(Base):
    __tablename__ = "pass_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    week_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weeks.id"), nullable=False)
    best_available_candidate_market_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prop_markets.id"), nullable=True)
    estimated_edge: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    uncertainty: Mapped[str | None] = mapped_column(String, nullable=True)
    reason_for_pass: Mapped[str] = mapped_column(String, nullable=False)
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # DATABASE.md v2 fix: v1 had week_id alone UNIQUE, which allowed
        # only one competitor total to PASS per week.
        UniqueConstraint("season_competitor_id", "week_id", name="one_pass_per_competitor_per_week"),
    )
