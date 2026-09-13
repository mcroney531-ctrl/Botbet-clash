"""Games/players/markets/quotes/snapshots persistence (DATABASE.md §2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.markets import Game, MarketSnapshot, Player, PropMarket, PropQuote


class MarketRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_game(
        self,
        *,
        external_ref: str,
        season_id: uuid.UUID,
        week_number: int,
        home_team: str,
        away_team: str,
        kickoff_at: datetime,
        status: str = "SCHEDULED",
    ) -> Game:
        row = Game(
            external_ref=external_ref,
            season_id=season_id,
            week_number=week_number,
            home_team=home_team,
            away_team=away_team,
            kickoff_at=kickoff_at,
            status=status,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_game(self, game_id: uuid.UUID) -> Game:
        row = self.session.get(Game, game_id)
        if row is None:
            raise LookupError(f"game {game_id} not found")
        return row

    def games_for_week(self, season_id: uuid.UUID, week_number: int) -> list[Game]:
        stmt = select(Game).where(Game.season_id == season_id, Game.week_number == week_number).order_by(Game.kickoff_at)
        return list(self.session.execute(stmt).scalars())

    def create_player(self, *, external_ref: str, name: str, team: str, position: str) -> Player:
        row = Player(external_ref=external_ref, name=name, team=team, position=position)
        self.session.add(row)
        self.session.flush()
        return row

    def create_prop_market(self, *, game_id: uuid.UUID, player_id: uuid.UUID, stat_type: str) -> PropMarket:
        row = PropMarket(game_id=game_id, player_id=player_id, stat_type=stat_type)
        self.session.add(row)
        self.session.flush()
        return row

    def get_prop_market(self, market_id: uuid.UUID) -> PropMarket:
        row = self.session.get(PropMarket, market_id)
        if row is None:
            raise LookupError(f"prop_market {market_id} not found")
        return row

    def markets_for_game(self, game_id: uuid.UUID) -> list[PropMarket]:
        stmt = select(PropMarket).where(PropMarket.game_id == game_id)
        return list(self.session.execute(stmt).scalars())

    def add_quote(
        self,
        *,
        market_id: uuid.UUID,
        sportsbook: str,
        line: Decimal,
        over_price: int,
        under_price: int,
        retrieved_at: datetime,
    ) -> PropQuote:
        row = PropQuote(
            market_id=market_id,
            sportsbook=sportsbook,
            line=line,
            over_price=over_price,
            under_price=under_price,
            retrieved_at=retrieved_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def quotes_as_of(self, market_id: uuid.UUID, as_of: datetime) -> list[PropQuote]:
        """Every quote for this market retrieved at or before `as_of` —
        the raw input a MarketSnapshotService needs to freeze a canonical
        read at a point in time (ARCHITECTURE.md §4, atomic checkpoints)."""

        stmt = (
            select(PropQuote)
            .where(PropQuote.market_id == market_id, PropQuote.retrieved_at <= as_of)
            .order_by(PropQuote.retrieved_at.desc())
        )
        return list(self.session.execute(stmt).scalars())

    def add_market_snapshot(self, row: MarketSnapshot) -> MarketSnapshot:
        self.session.add(row)
        self.session.flush()
        return row

    def get_market_snapshot(self, snapshot_id: uuid.UUID) -> MarketSnapshot:
        row = self.session.get(MarketSnapshot, snapshot_id)
        if row is None:
            raise LookupError(f"market_snapshot {snapshot_id} not found")
        return row

    def latest_snapshot(self, market_id: uuid.UUID) -> MarketSnapshot | None:
        stmt = select(MarketSnapshot).where(MarketSnapshot.market_id == market_id).order_by(MarketSnapshot.taken_at.desc())
        return self.session.execute(stmt).scalars().first()
