"""DATABASE.md §1 — season, rules, competitors."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (CheckConstraint("status IN ('DRAFT','ACTIVE','COMPLETE')", name="valid_status"),)


class SeasonRules(Base):
    """Append-only/versioned (DATABASE.md §1). A rule amendment is a new
    row with `superseded_by` pointing to it — never an UPDATE."""

    __tablename__ = "season_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    rules_version: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    starting_bankroll_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_sportsbook: Mapped[str] = mapped_column(String, nullable=False)
    # Which market-data provider supplies this season's quote history.
    # Frozen alongside canonical_sportsbook and for the same reason: a
    # midseason vendor switch must not be able to silently change the
    # research baseline by blending two feeds into one consensus. Quote
    # selection pins on this value.
    market_data_provider: Mapped[str] = mapped_column(String, nullable=False)
    research_settlement_provider: Mapped[str] = mapped_column(String, nullable=False)
    research_settlement_delay_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    supported_prop_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    devig_method: Mapped[str] = mapped_column(String, nullable=False)
    benchmark_slate_size: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_methodology: Mapped[str] = mapped_column(String, nullable=False)
    checkpoint_windows: Mapped[dict] = mapped_column(JSONB, nullable=False)
    kelly_fraction: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    standard_max_bankroll_fraction: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    exceptional_max_bankroll_fraction: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    minimum_stake_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stake_increment_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pounce_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    attribution_confidence_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    weekly_decision_deadline_rule: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    competition_requires_nonpushable_line: Mapped[bool] = mapped_column(default=True, nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("season_rules.id"), nullable=True)
    amendment_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    effective_from: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class Week(Base):
    __tablename__ = "weeks"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_real_money: Mapped[bool] = mapped_column(nullable=False)
    counts_toward_standings: Mapped[bool] = mapped_column(nullable=False, default=True)
    counts_toward_awards: Mapped[bool] = mapped_column(nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    opened_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    research_locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("season_id", "week_number", name="season_week_number"),)


class Competitor(Base):
    """Cross-season identity only — DATABASE.md §1's v2 fix. Never holds
    model_version/status/bankroll; those are season-scoped, on
    `SeasonCompetitor`."""

    __tablename__ = "competitors"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'openai' | 'anthropic' | 'google'
    provider: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class SeasonCompetitor(Base):
    """The actual competitive instance for one season. Every competitive
    table (bankroll, tickets, wagers, forecasts, events...) points here,
    never at `Competitor.id` directly."""

    __tablename__ = "season_competitors"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    competitor_id: Mapped[str] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String, nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    provider_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    frozen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    competitor: Mapped[Competitor] = relationship()

    __table_args__ = (
        UniqueConstraint("season_id", "competitor_id", name="one_instance_per_season"),
        CheckConstraint("status IN ('ACTIVE','BUSTED')", name="valid_status"),
    )
