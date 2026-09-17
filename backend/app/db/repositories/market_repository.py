"""Games/players/markets/quotes/snapshots persistence (DATABASE.md §2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.markets import Game, MarketSnapshot, Player, PropMarket, PropQuote
from app.db.models.roster import GamePlayer
from app.rosterdata.teams import CanonicalTeam, TeamMappingError
from app.marketdata.provenance import (
    SYNTHETIC_PARSER_VERSION,
    SYNTHETIC_SOURCE,
    ensure_synthetic_provenance,
    legacy_fingerprint,
)


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
        home_team_canonical: str | None = None,
        away_team_canonical: str | None = None,
    ) -> Game:
        """Create a game.

        `home_team` / `away_team` are whatever the source called them --
        for The Odds API that is a display name like "Buffalo Bills". The
        canonical columns are what ALL logic reads; real ingestion passes
        them explicitly after mapping through the provider table.

        When they are omitted, the display value must already BE a
        canonical code (which is true of every synthetic fixture). It is
        validated rather than assumed: an unmappable value raises here
        instead of silently writing a team that no opponent derivation can
        match.
        """

        home_canonical = home_team_canonical or _canonical_or_raise(home_team, "home_team")
        away_canonical = away_team_canonical or _canonical_or_raise(away_team, "away_team")
        row = Game(
            external_ref=external_ref,
            season_id=season_id,
            week_number=week_number,
            home_team=home_team,
            away_team=away_team,
            home_team_canonical=home_canonical,
            away_team_canonical=away_canonical,
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

    def create_game_player(
        self,
        *,
        game_id: uuid.UUID,
        player_id: uuid.UUID,
        team: str,
        position: str | None = None,
    ) -> GamePlayer:
        """The game-scoped team relationship, which is what the orchestrator
        reads to derive `opponent`.

        Real ingestion creates this through the roster identity service,
        which also appends a GamePlayerObservation carrying the roster
        snapshot's provenance. This bare constructor exists for synthetic
        fixtures, which have no roster provider behind them.
        """

        row = GamePlayer(
            game_id=game_id,
            player_id=player_id,
            team=_canonical_or_raise(team, "team"),
            position=position,
        )
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
        as_of_at: datetime | None = None,
    ) -> PropQuote:
        """Write a SYNTHETIC quote — fabricated market state with no
        provider behind it.

        Real ingestion does not come through here: it goes through
        `app/marketdata/ingestion.py`, which carries genuine provider
        provenance and a real fingerprint. This path exists for tests and
        `live_smoke.py`, and it stamps every row it writes as SYNTHETIC so
        the two populations can never be confused in a query. Quote
        selection is source-pinned, so these rows are also invisible to a
        season configured against a real provider.
        """

        as_of = as_of_at if as_of_at is not None else retrieved_at
        if as_of > retrieved_at:
            raise ValueError("as_of_at must not be later than retrieved_at")

        # The id is generated here rather than left to the column default:
        # `uuid_pk()`'s `default=uuid.uuid4` is a SQLAlchemy column default,
        # evaluated at INSERT, so `row.id` is still None at construction --
        # and the legacy fingerprint is derived from the id, so reading it
        # early would hash "legacy:None" and collide every synthetic quote
        # against the UNIQUE constraint.
        quote_id = uuid.uuid4()
        row = PropQuote(
            id=quote_id,
            market_id=market_id,
            sportsbook=sportsbook,
            line=line,
            over_price=over_price,
            under_price=under_price,
            as_of_at=as_of,
            retrieved_at=retrieved_at,
            source=SYNTHETIC_SOURCE,
            provider_call_id=ensure_synthetic_provenance(self.session),
            parser_version=SYNTHETIC_PARSER_VERSION,
            fingerprint=legacy_fingerprint(quote_id),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def quotes_as_of(self, market_id: uuid.UUID, *, source: str, as_of: datetime) -> list[PropQuote]:
        """Every quote for this market that was *observed* at or before
        `as_of`, from the pinned market-data source — the raw input
        MarketSnapshotService needs to freeze a canonical read at a point
        in time (ARCHITECTURE.md §4, atomic checkpoints).

        Filters on `as_of_at`, never `retrieved_at`. Those are different
        facts (seam doc §3): a backfill retrieved on 20 September for a
        5 September snapshot represents 5 September market state. Ordering
        by retrieval would exclude it from the checkpoint it belongs to and
        silently admit it to every later one, presenting two-week-old
        prices as current.

        `source` is required rather than optional so that adding a second
        provider cannot quietly blend two feeds into one consensus — the
        caller must always say which history it means.

        Ordering is fully deterministic: `as_of_at` decides, `retrieved_at`
        breaks a tie between the same market state observed twice, and `id`
        guarantees a stable answer when even that ties. Callers downstream
        take the first row per sportsbook, so an unstable sort here would
        make the canonical baseline — and every de-vigged probability built
        on it — non-reproducible.
        """

        stmt = (
            select(PropQuote)
            .where(
                PropQuote.market_id == market_id,
                PropQuote.source == source,
                PropQuote.as_of_at <= as_of,
            )
            .order_by(
                PropQuote.as_of_at.desc(),
                PropQuote.retrieved_at.desc(),
                PropQuote.id.desc(),
            )
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


def _canonical_or_raise(value: str, field_name: str) -> str:
    try:
        return CanonicalTeam(value.strip().upper()).value
    except ValueError:
        raise TeamMappingError(
            f"{field_name}={value!r} is not a CanonicalTeam code and no explicit "
            f"{field_name}_canonical was supplied. Map it through the provider's "
            "team table in app/rosterdata/teams.py -- never guess."
        ) from None
