"""DATABASE.md §2 (games/players/markets/quotes) and §4 (checkpoint runs)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk


class Game(Base):
    __tablename__ = "games"

    id: Mapped[uuid.UUID] = uuid_pk()
    external_ref: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Vendor display strings, retained for the Show layer and diagnosis.
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    # The canonical vocabulary. ALL logic uses these -- see CanonicalTeam.
    home_team_canonical: Mapped[str] = mapped_column(String, nullable=False)
    away_team_canonical: Mapped[str] = mapped_column(String, nullable=False)
    kickoff_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED")
    created_at: Mapped[datetime] = created_at_column()


class Player(Base):
    """A PERSON, not a person-on-a-team.

    `external_ref` is the durable identity: "GSIS:<gsis_id>" for real
    players resolved from the roster provider. nflverse resolves it; GSIS
    *is* it, so swapping roster providers later does not orphan these rows.

    `team` and `position` are NULLABLE and explicitly NON-AUTHORITATIVE.
    A player's team is time- and game-dependent, so it lives on
    `GamePlayer`. These columns are retained only so pre-Phase-4 rows and
    synthetic fixtures keep working; nothing in the research path may read
    them. The `opponent` bug in orchestrator.py is exactly what happens
    when a "conveniently populated" legacy field gets treated as
    authoritative, which is why they were loosened rather than left
    NOT NULL and quietly filled.
    """

    __tablename__ = "players"

    id: Mapped[uuid.UUID] = uuid_pk()
    external_ref: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    team: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class PropMarket(Base):
    """Conceptual market, book-agnostic. Deliberately has no line/price/
    pushability field — see DATABASE.md §2's note on why that's a
    per-snapshot property, not a market property."""

    __tablename__ = "prop_markets"

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    stat_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    # The natural key. Real ingestion get-or-creates against this on every
    # poll, so without the constraint a concurrent or retried run would
    # fork a market in two and split its quote history.
    __table_args__ = (UniqueConstraint("game_id", "player_id", "stat_type"),)


class PropQuote(Base):
    """Append-only, immutable per observation — never overwritten.

    A row means: *at `as_of_at`, this provider showed us this book /
    player / stat / line / price state.* It deliberately does NOT mean
    "the market last changed at this time" — that is
    `provider_market_updated_at`, which is diagnostic only and must never
    drive checkpoint selection (see the seam doc §3).

    Repeated unchanged observations are RETAINED (§4). Keeping only the
    first of two identical quotes would make "market genuinely unchanged
    and freshly observed" indistinguishable from "our feed stopped seeing
    that book," which is exactly a checkpoint-freshness question.
    """

    __tablename__ = "prop_quotes"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    sportsbook: Mapped[str] = mapped_column(String, nullable=False)
    line: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    over_price: Mapped[int] = mapped_column(Integer, nullable=False)
    under_price: Mapped[int] = mapped_column(Integer, nullable=False)
    # The time this observation represents. Current pull: one stable
    # capture timestamp for the accepted fetch. Historical pull: the
    # provider's returned snapshot time, never our clock. This is the
    # field quote selection filters on.
    as_of_at: Mapped[datetime] = mapped_column(nullable=False)
    # When our process received and persisted the response. Operational
    # provenance; never used for market-state eligibility.
    retrieved_at: Mapped[datetime] = mapped_column(nullable=False)
    # Vendor market-level `last_update`. Diagnostic/freshness metadata only.
    provider_market_updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Which market-data provider supplied this. Pinned against
    # SeasonRules.market_data_provider so a vendor switch cannot silently
    # blend two feeds into one consensus.
    source: Mapped[str] = mapped_column(String, nullable=False)
    provider_call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_calls.id"), nullable=False)
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=True)
    # Provenance ONLY. Quote selection does not filter by this: a global
    # "newest parser wins" filter would let a v2 deployment erase every
    # week still parsed by v1 until each archived response was replayed,
    # and a bug fix must never be able to delete history. The
    # supersession policy is deferred until parser replay actually exists.
    parser_version: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class MarketSnapshot(Base):
    """Frozen consensus/canonical read at a point in time."""

    __tablename__ = "market_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(nullable=False)
    canonical_sportsbook: Mapped[str] = mapped_column(String, nullable=False)
    canonical_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    canonical_over_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_under_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_over_probability: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    canonical_under_probability: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    devig_method: Mapped[str] = mapped_column(String, nullable=False)
    same_line_consensus_over_probability: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    market_median_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    market_min_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    market_max_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    number_of_books: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_valid_canonical_baseline: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            "canonical_over_probability IS NULL OR (canonical_over_probability >= 0 AND canonical_over_probability <= 1)",
            name="canonical_over_probability_range",
        ),
        CheckConstraint(
            "canonical_under_probability IS NULL OR (canonical_under_probability >= 0 AND canonical_under_probability <= 1)",
            name="canonical_under_probability_range",
        ),
        CheckConstraint(
            "same_line_consensus_over_probability IS NULL OR (same_line_consensus_over_probability >= 0 AND same_line_consensus_over_probability <= 1)",
            name="consensus_probability_range",
        ),
    )


class CheckpointRun(Base):
    """DATABASE.md §4 / ARCHITECTURE.md §4: the actual capture unit for
    OPENING/MID/FINAL is per game, not per week."""

    __tablename__ = "checkpoint_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    checkpoint_type: Mapped[str] = mapped_column(String, nullable=False)
    window_start: Mapped[datetime] = mapped_column(nullable=False)
    window_end: Mapped[datetime] = mapped_column(nullable=False)
    target_time: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    captured_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("game_id", "checkpoint_type", name="one_run_per_game_checkpoint"),
    )
