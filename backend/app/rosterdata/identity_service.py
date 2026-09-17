"""Persisting resolved player identity.

The durable relationship and its provenance are deliberately two tables
(seam §4). `GamePlayer` records what BotBet Clash ACCEPTED; each
`GamePlayerObservation` records why and when we accepted or revalidated
it.

Collapsing them breaks at the second checkpoint: OPENING resolves a player
from snapshot A, FINAL revalidates from snapshot B, and a single
provenance column must then either overwrite -- destroying the evidence
that supported OPENING -- or go stale, hiding that FINAL revalidated at
all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.markets import Player
from app.db.models.roster import GamePlayer, GamePlayerObservation
from app.rosterdata.base import PlayerResolution, RosterSnapshot
from app.rosterdata.resolution import RESOLVER_VERSION, stable_player_external_ref


class RosterIdentityConflict(RuntimeError):
    """A later observation disagrees with the accepted relationship.

    Never resolved by mutating GamePlayer. A player's team changing
    between checkpoints is either a real transaction we must review or a
    resolution error we must investigate; silently rewriting the accepted
    relationship would erase the question.
    """


def resolve_and_record(
    session: Session,
    *,
    game_id: uuid.UUID,
    resolution: PlayerResolution,
    display_name: str,
    snapshot: RosterSnapshot,
    roster_provider_call_id: uuid.UUID,
    observed_at: datetime,
) -> GamePlayer:
    """Upsert Player + GamePlayer, and ALWAYS append an observation.

    Appending on every successful resolution -- including one that merely
    agrees with what we already accepted -- is the point: it is what
    distinguishes "revalidated at FINAL" from "never checked again".
    """

    if not resolution.resolved or resolution.stable_id is None or resolution.team is None:
        raise ValueError(
            f"refusing to persist an unresolved identity ({resolution.outcome}): "
            f"{resolution.detail}"
        )

    external_ref = stable_player_external_ref(resolution.stable_id)
    player = session.execute(
        select(Player).where(Player.external_ref == external_ref)
    ).scalar_one_or_none()
    if player is None:
        # team/position are NOT set here. Player is a person; the
        # game-scoped team lives on GamePlayer, and populating the legacy
        # columns "for convenience" is exactly how the opponent bug
        # happened.
        player = Player(external_ref=external_ref, name=display_name)
        session.add(player)
        session.flush()

    game_player = session.execute(
        select(GamePlayer).where(
            GamePlayer.game_id == game_id, GamePlayer.player_id == player.id
        )
    ).scalar_one_or_none()

    if game_player is None:
        game_player = GamePlayer(
            game_id=game_id,
            player_id=player.id,
            team=resolution.team.value,
            position=resolution.position,
        )
        session.add(game_player)
        session.flush()
    elif game_player.team != resolution.team.value:
        raise RosterIdentityConflict(
            f"{display_name} ({external_ref}) was accepted as "
            f"{game_player.team} for game {game_id}, but a later roster "
            f"observation says {resolution.team.value}. Not rewriting the "
            "accepted relationship -- this needs review."
        )

    session.add(
        GamePlayerObservation(
            game_player_id=game_player.id,
            roster_provider_call_id=roster_provider_call_id,
            roster_season=snapshot.season,
            roster_week=snapshot.week,
            roster_basis=snapshot.basis,
            resolved_team=resolution.team.value,
            resolved_position=resolution.position,
            resolver_version=RESOLVER_VERSION,
            observed_at=observed_at,
        )
    )
    session.flush()
    return game_player
