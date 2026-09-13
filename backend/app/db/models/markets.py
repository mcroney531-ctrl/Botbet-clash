"""DATABASE.md §2 (games/players/markets/quotes) and §4 (checkpoint runs)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk


class Game(Base):
    __tablename__ = "games"

    id: Mapped[uuid.UUID] = uuid_pk()
    external_ref: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    kickoff_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED")
    created_at: Mapped[datetime] = created_at_column()


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = uuid_pk()
    external_ref: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    team: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[str] = mapped_column(String, nullable=False)
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


class PropQuote(Base):
    """Append-only, immutable per snapshot — never overwritten."""

    __tablename__ = "prop_quotes"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prop_markets.id"), nullable=False)
    sportsbook: Mapped[str] = mapped_column(String, nullable=False)
    line: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    over_price: Mapped[int] = mapped_column(Integer, nullable=False)
    under_price: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(nullable=False)


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
