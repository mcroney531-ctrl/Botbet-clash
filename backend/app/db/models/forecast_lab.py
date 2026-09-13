"""DATABASE.md §5 (evidence/forecasts), §5's benchmark plan/slots, §6 (AI
orchestration/reproducibility)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk


class EvidenceSnapshot(Base):
    """Immutable. Every forecast points to exactly one of these."""

    __tablename__ = "evidence_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    checkpoint_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("checkpoint_runs.id"), nullable=True)
    checkpoint_type: Mapped[str | None] = mapped_column(String, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    market_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("market_snapshots.id"), nullable=False)


class ForecastObservation(Base):
    """Append-only; a revision is a new row referencing `revision_parent_id`,
    never an UPDATE of an earlier one."""

    __tablename__ = "forecast_observations"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)  # BENCHMARK | OPEN_MARKET
    checkpoint_type: Mapped[str | None] = mapped_column(String, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(nullable=False)
    model_probability_over: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    canonical_market_probability_over: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    same_line_consensus_probability_over: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    canonical_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    canonical_over_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_under_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_median_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    probability_disagreement: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    uncertainty: Mapped[str] = mapped_column(String, nullable=False)
    evidence_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=False)
    revision_parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("forecast_observations.id"), nullable=True)
    revision_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    research_eligible: Mapped[bool] = mapped_column(nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("model_probability_over >= 0 AND model_probability_over <= 1", name="probability_range"),
        CheckConstraint(
            "canonical_market_probability_over IS NULL OR (canonical_market_probability_over >= 0 AND canonical_market_probability_over <= 1)",
            name="canonical_probability_range",
        ),
        CheckConstraint(
            "same_line_consensus_probability_over IS NULL OR (same_line_consensus_probability_over >= 0 AND same_line_consensus_probability_over <= 1)",
            name="consensus_probability_range",
        ),
        CheckConstraint("confidence >= 1 AND confidence <= 10", name="confidence_range"),
    )


class BenchmarkSlatePlan(Base):
    """Committed once per week, before any game's OPENING window opens."""

    __tablename__ = "benchmark_slate_plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    week_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weeks.id"), unique=True, nullable=False)
    target_slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_method: Mapped[str] = mapped_column(String, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(nullable=False)


class BenchmarkSlot(Base):
    """One row per planned slot; resolved asynchronously per game."""

    __tablename__ = "benchmark_slots"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("benchmark_slate_plans.id"), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    target_stat_type: Mapped[str] = mapped_column(String, nullable=False)
    fallback_stat_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    resolved_market_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prop_markets.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (UniqueConstraint("plan_id", "slot_index", name="one_slot_per_index"),)


class AgentSession(Base):
    """One row per consequential AI call (constitution §106)."""

    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    season_competitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season_competitors.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_identifier: Mapped[str] = mapped_column(String, nullable=False)
    call_type: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    evidence_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_snapshots.id"), nullable=True)
    market_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("market_snapshots.id"), nullable=True)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validated_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_valid: Mapped[bool] = mapped_column(nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bankroll_at_decision_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(nullable=False)


class AgentSessionEvidenceSnapshot(Base):
    """Multi-snapshot inputs for a batched call — see DATABASE.md §6's
    batching fix."""

    __tablename__ = "agent_session_evidence_snapshots"

    agent_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id"), primary_key=True)
    evidence_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_snapshots.id"), primary_key=True)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
