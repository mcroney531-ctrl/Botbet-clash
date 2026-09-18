"""DATABASE.md §5 (evidence/forecasts), §5's benchmark plan/slots, §6 (AI
orchestration/reproducibility)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    """Committed once per week, before any game's OPENING window opens.

    Phase 4A.7 made the plan answer one question it previously could not:
    WHAT COMPLETE FIXTURE POOL DID THE ALLOCATOR SEE? Without that, a plan
    could only be re-derived by re-fetching a schedule release that may
    since have changed, so "this sample was precommitted" was an
    assertion rather than a reproducible fact.

    The provenance columns are nullable because Phase-2B synthetic plans
    legitimately have no provider call behind them. `is_official` is the
    discriminator, and the CHECK below makes an official plan without its
    provenance impossible at the DATABASE level rather than merely
    discouraged in the service.
    """

    __tablename__ = "benchmark_slate_plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    week_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("weeks.id"), unique=True, nullable=False)
    target_slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_method: Mapped[str] = mapped_column(String, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(nullable=False)

    # --- Phase 4A.7 provenance ---------------------------------------
    is_official: Mapped[bool] = mapped_column(nullable=False, default=False)
    rules_version: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule_provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_calls.id"), nullable=True
    )
    resolver_version: Mapped[str | None] = mapped_column(String, nullable=True)
    fixture_key_version: Mapped[str | None] = mapped_column(String, nullable=True)
    fixture_pool_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sha256 over the canonical ordered fixture keys -- never row ids, never
    # kickoff times. This is what survives a clean database rebuild.
    fixture_pool_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The COMPLETE planning input: keys AND UTC-normalized kickoffs. The pool
    # fingerprint answers "which fixtures"; this one answers "what did the
    # allocator and the deadline actually see". Kickoff is not decorative at
    # commit time -- it is what earliest_opening_at is computed from.
    planning_input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The deadline this commitment was checked against, kept so the check
    # can be audited later rather than merely trusted.
    earliest_opening_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "NOT is_official OR ("
            " rules_version IS NOT NULL"
            " AND schedule_provider_call_id IS NOT NULL"
            " AND resolver_version IS NOT NULL"
            " AND fixture_key_version IS NOT NULL"
            " AND fixture_pool_fingerprint IS NOT NULL"
            " AND planning_input_fingerprint IS NOT NULL"
            " AND fixture_pool_count IS NOT NULL"
            " AND earliest_opening_at IS NOT NULL)",
            name="official_plan_requires_provenance",
        ),
    )


class BenchmarkSlateFixture(Base):
    """The COMPLETE fixture pool the allocator saw, frozen under its plan.

    All sixteen of a week's fixtures, not just the five selected. Owned by
    the plan rather than kept in a global table on purpose: this is a
    historical artifact of one commitment, and a shared mutable
    `ScheduledFixture` table would become a second source of truth that
    later schedule releases could rewrite underneath a committed sample.

    `game_id` is NULL until the market provider posts the event and
    `game_registration` BINDS it. Binding moves that one column and
    nothing else -- the planned week, teams and kickoff are what the
    allocator actually saw, and rewriting them to match a later schedule
    release would destroy the record it exists to keep.
    """

    __tablename__ = "benchmark_slate_fixtures"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_slate_plans.id"), nullable=False
    )
    # The canonical key: season:game_type:Www:AWAY@HOME. Kickoff is
    # deliberately not part of it -- broadcast times move, fixtures do not.
    fixture_key: Mapped[str] = mapped_column(String, nullable=False)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    away_team_canonical: Mapped[str] = mapped_column(String, nullable=False)
    home_team_canonical: Mapped[str] = mapped_column(String, nullable=False)
    planned_kickoff_at: Mapped[datetime] = mapped_column(nullable=False)
    game_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    bound_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("plan_id", "fixture_key", name="one_fixture_per_plan"),
    )


class BenchmarkSlot(Base):
    """One row per SELECTED slot; resolved asynchronously per game.

    `slate_fixture_id` replaced `game_id` as the link to what the slot is
    about. A slot now points at a planned FIXTURE, which exists whether or
    not the odds provider has posted the event -- that coupling is what let
    two unlisted Week-3 events change a slate nflverse already knew all
    sixteen fixtures for.

    `game_id` remains, nullable, for pre-4A.7 rows only. Nothing written
    by the current core sets it.
    """

    __tablename__ = "benchmark_slots"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("benchmark_slate_plans.id"), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    slate_fixture_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_slate_fixtures.id"), nullable=True
    )
    game_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    target_stat_type: Mapped[str] = mapped_column(String, nullable=False)
    fallback_stat_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    resolved_market_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prop_markets.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # How a slot reaches its game: through the planned fixture, which holds
    # the binding. Deliberately not a denormalized `game_id` on the slot --
    # two columns naming the same game is two places for it to be wrong.
    slate_fixture: Mapped["BenchmarkSlateFixture | None"] = relationship(lazy="joined")

    __table_args__ = (UniqueConstraint("plan_id", "slot_index", name="one_slot_per_index"),)

    @property
    def bound_game_id(self) -> uuid.UUID | None:
        """The game this slot is about, or None until the provider posts it."""

        if self.slate_fixture is not None:
            return self.slate_fixture.game_id
        return self.game_id  # pre-4A.7 rows only


class AgentSession(Base):
    """One row per consequential AI call (constitution §106).

    Phase 3 lifecycle (ARCHITECTURE.md's "no DB transaction can span an
    external network call" rule): a row is created `PENDING` — with its
    inputs already registered via `agent_session_evidence_snapshots` —
    and committed *before* the provider is ever called, so a crash
    mid-call leaves a discoverable, recoverable row rather than nothing
    at all. `raw_response` and `is_valid` are therefore nullable: they
    aren't known until the call returns.
    """

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

    # -- lifecycle (Phase 3) -----------------------------------------------
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    orchestration_key: Mapped[str] = mapped_column(String, nullable=False)
    rendered_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    usage_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    transport_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correction_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    validated_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_valid: Mapped[bool | None] = mapped_column(nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bankroll_at_decision_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','CALLING','VALID','INVALID','FAILED')", name="valid_status"
        ),
        CheckConstraint(
            "error_category IS NULL OR error_category IN ("
            "'AUTHENTICATION_ERROR','RATE_LIMITED','TIMEOUT','PROVIDER_UNAVAILABLE',"
            "'INVALID_PROVIDER_RESPONSE','SCHEMA_VALIDATION_FAILED','CONTENT_REFUSAL','UNKNOWN_PROVIDER_ERROR')",
            name="valid_error_category",
        ),
        UniqueConstraint("orchestration_key", name="one_session_per_orchestration_key"),
    )


class AgentSessionEvidenceSnapshot(Base):
    """Multi-snapshot inputs for a batched call — see DATABASE.md §6's
    batching fix."""

    __tablename__ = "agent_session_evidence_snapshots"

    agent_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id"), primary_key=True)
    evidence_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_snapshots.id"), primary_key=True)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
