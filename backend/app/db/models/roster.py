"""Game-scoped player identity and its resolution history.

Two tables, and the split is the point (phase4-roster-identity-seam.md §4).

`GamePlayer` is the durable relationship BotBet Clash ACCEPTED: this
person, in this game, on this team.

`GamePlayerObservation` is why and when we accepted or revalidated it:
which roster snapshot, from which provider call, under which resolver
version.

Collapsing these into one row breaks at the second checkpoint. OPENING
resolves Goff from snapshot A; FINAL revalidates from snapshot B. A single
provenance column must then either be overwritten -- destroying the record
of what supported OPENING -- or left stale, hiding the fact that FINAL
revalidated at all. Neither is acceptable in a reproducibility-first
system, so observations are append-only and the relationship is stable.

A later observation that DISAGREES never silently mutates GamePlayer. It
raises ROSTER_IDENTITY_CONFLICT and quarantines for review.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk

ROSTER_BASIS_VALUES = (
    "CURRENT_CONTEMPORANEOUS",
    "ARCHIVED_CONTEMPORANEOUS",
    "HISTORICAL_RECONSTRUCTED",
)


class GamePlayer(Base):
    """The accepted relationship. Team is authoritative HERE, not on Player."""

    __tablename__ = "game_players"

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"), nullable=False)
    team: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (UniqueConstraint("game_id", "player_id"),)


class GamePlayerObservation(Base):
    """Append-only. One row per resolution or revalidation.

    `roster_basis` lives here rather than on GamePlayer because it
    describes THIS observation's evidentiary weight -- OPENING may be
    CURRENT_CONTEMPORANEOUS while a later backfill of the same
    relationship is HISTORICAL_RECONSTRUCTED, and the relationship itself
    is neither.
    """

    __tablename__ = "game_player_observations"

    id: Mapped[uuid.UUID] = uuid_pk()
    game_player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("game_players.id"), nullable=False)
    roster_provider_call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_calls.id"), nullable=False
    )
    roster_season: Mapped[int] = mapped_column(Integer, nullable=False)
    roster_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roster_basis: Mapped[str] = mapped_column(String, nullable=False)
    resolved_team: Mapped[str] = mapped_column(String, nullable=False)
    resolved_position: Mapped[str | None] = mapped_column(String, nullable=True)
    # The roster equivalent of PropQuote.parser_version: if normalization or
    # resolution logic changes, we must know which logic produced an old
    # observation. Provenance only -- nothing filters on it.
    resolver_version: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            "roster_basis IN ('CURRENT_CONTEMPORANEOUS','ARCHIVED_CONTEMPORANEOUS',"
            "'HISTORICAL_RECONSTRUCTED')",
            name="valid_roster_basis",
        ),
    )
