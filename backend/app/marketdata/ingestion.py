"""IngestionService — the only writer of Game / Player / PropMarket /
PropQuote from external data (seam doc §12).

Provider adapters never touch the database. They turn vendor payloads
into DTOs and stop. Everything about how those DTOs become our rows --
identity resolution, fingerprinting, dedup, quarantine, partial-success
accounting -- lives here, so swapping providers cannot change our
persistence conventions.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ingestion import IngestionRun, ProviderCall
from app.db.models.markets import Game, Player, PropMarket, PropQuote
from app.marketdata.base import ProviderDiagnostic
from app.marketdata.dto import ProviderQuote

PARSER_VERSION = "the-odds-api-v4-parser-v1"
"""Bumped when normalization changes in a way that could produce a
different quote from the same bytes. Provenance only: quote selection
does NOT filter on it, because a global "newest parser wins" filter would
make every week still parsed by the old version vanish the moment a new
one deployed. See the seam doc §6."""


def canonical_line(line: Decimal) -> str:
    """Stable textual form of a line for fingerprinting.

    Decimal("74.50") and Decimal("74.5") are the same number but different
    strings, and PropQuote stores Numeric(6,2) so a round-trip can change
    which one you hold. Without normalizing, the same quote read back from
    the database would fingerprint differently from the one just parsed,
    and replay would stop being idempotent.
    """

    normalized = line.normalize()
    # normalize() renders small integers in exponent form (5E+1); quantize
    # back to a plain representation.
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def quote_fingerprint(
    *,
    provider_call_id: uuid.UUID,
    external_event_id: str,
    player_external_ref: str,
    stat_family: str,
    sportsbook: str,
    line: Decimal,
    over_price: int,
    under_price: int,
    parser_version: str,
) -> str:
    """Identity of one normalized observation within one provider call.

    Keyed on the CALL, not on a timestamp. Reprocessing a stored response
    reuses its original call id and is therefore idempotent; a genuinely
    new poll is a new call id and legitimately records another
    observation even when no price moved. That is what lets us keep
    repeated unchanged observations (seam §4) without duplicate spam from
    retries.
    """

    parts = (
        str(provider_call_id),
        external_event_id,
        player_external_ref,
        str(stat_family),
        sportsbook,
        canonical_line(line),
        str(int(over_price)),
        str(int(under_price)),
        parser_version,
    )
    # Unit separator: not legal inside any of these fields, so no value can
    # impersonate a boundary and collide two different quotes.
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass
class IngestionOutcome:
    quotes_observed: int = 0
    quotes_written: int = 0
    quotes_deduplicated: int = 0
    markets_quarantined: int = 0
    diagnostics: list[ProviderDiagnostic] = field(default_factory=list)

    @property
    def had_quarantine(self) -> bool:
        return self.markets_quarantined > 0


def partition_ambiguous_lines(
    quotes: Sequence[ProviderQuote],
) -> tuple[list[ProviderQuote], list[ProviderDiagnostic]]:
    """Split quotes into (acceptable, quarantine diagnostics).

    If a single (sportsbook, player, stat_family) yields more than one
    candidate paired line within one call, the ENTIRE set is refused —
    not the first, not the lowest, not the median, not the one closest to
    even money.

    Downstream, MarketSnapshotService takes the first row per sportsbook
    to establish the canonical baseline. With simultaneous alternates
    present, "first" is arbitrary ordering, so any pick-one rule would let
    row order silently decide the canonical line and therefore every
    de-vigged probability the competitors are scored against. Losing one
    book's quote for one observation is the cheaper failure by far.

    Whether this vendor even exposes alternates within a single mapped
    market — as opposed to under separate market keys — is an OPEN
    question for the validation probe.
    """

    grouped: dict[tuple[str, str, str], list[ProviderQuote]] = defaultdict(list)
    for q in quotes:
        grouped[(q.sportsbook, q.player.as_external_ref(), str(q.stat_family))].append(q)

    accepted: list[ProviderQuote] = []
    diagnostics: list[ProviderDiagnostic] = []
    for (book, _player_ref, family), group in grouped.items():
        distinct_lines = {canonical_line(q.line) for q in group}
        if len(group) > 1 and len(distinct_lines) > 1:
            diagnostics.append(
                ProviderDiagnostic(
                    category="AMBIGUOUS_ALTERNATE_LINE",
                    detail=(
                        f"{len(group)} candidate lines ({', '.join(sorted(distinct_lines))}) "
                        f"for {family} and no vendor signal identifying the standard "
                        "market; quarantining the whole set rather than picking one"
                    ),
                    vendor_market_key=group[0].vendor_market_key,
                    sportsbook=book,
                    player_display_name=group[0].player.display_name,
                )
            )
            continue
        # Same line repeated within one call is a vendor duplicate, not an
        # alternate; the fingerprint collapses it on insert.
        accepted.extend(group)
    return accepted, diagnostics


class IngestionService:
    def __init__(self, session: Session, *, parser_version: str = PARSER_VERSION) -> None:
        self.session = session
        self.parser_version = parser_version

    # ---------------------------------------------------------------
    # Identity resolution. All get-or-create, because every poll re-sees
    # the same events, players and markets.
    # ---------------------------------------------------------------

    def resolve_game(
        self,
        *,
        external_ref: str,
        season_id: uuid.UUID,
        week_number: int,
        home_team: str,
        away_team: str,
        kickoff_at,
    ) -> Game:
        existing = self.session.execute(
            select(Game).where(Game.external_ref == external_ref)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Game(
            external_ref=external_ref,
            season_id=season_id,
            week_number=week_number,
            home_team=home_team,
            away_team=away_team,
            kickoff_at=kickoff_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def resolve_player(self, *, external_ref: str, name: str, team: str, position: str) -> Player:
        """Get-or-create a Player.

        `team` and `position` are NOT NULL in the current schema and this
        method will not invent them. Callers must supply real values —
        placeholder strings like "UNKNOWN" are prohibited outright, since
        fabricated roster data is invisible at query time and would
        silently corrupt the research record. Whether this provider even
        supplies team/position is an OPEN probe question; until it is
        answered, production player persistence stays blocked.
        """

        for value, label in ((team, "team"), (position, "position")):
            stripped = (value or "").strip()
            if not stripped or stripped.upper() in {"UNKNOWN", "N/A", "NONE", "TBD", "?"}:
                raise ValueError(
                    f"refusing to write Player.{label}={value!r}: placeholder roster "
                    "values are prohibited. Supply real data or leave the player "
                    "unpersisted and report the blocker."
                )
        existing = self.session.execute(
            select(Player).where(Player.external_ref == external_ref)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Player(external_ref=external_ref, name=name, team=team, position=position)
        self.session.add(row)
        self.session.flush()
        return row

    def resolve_prop_market(
        self, *, game_id: uuid.UUID, player_id: uuid.UUID, stat_type: str
    ) -> PropMarket:
        existing = self.session.execute(
            select(PropMarket).where(
                PropMarket.game_id == game_id,
                PropMarket.player_id == player_id,
                PropMarket.stat_type == stat_type,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = PropMarket(game_id=game_id, player_id=player_id, stat_type=stat_type)
        self.session.add(row)
        self.session.flush()
        return row

    # ---------------------------------------------------------------
    # Quote persistence.
    # ---------------------------------------------------------------

    def persist_quote(
        self,
        *,
        quote: ProviderQuote,
        market_id: uuid.UUID,
        provider_call: ProviderCall,
        run: IngestionRun | None = None,
    ) -> PropQuote | None:
        """Insert one observation, or None if it is a replay.

        Dedup is by fingerprint, which includes the provider call id: the
        same response reprocessed yields the same fingerprint and is
        skipped, while a new poll yields a new one and is written even if
        every price is identical.
        """

        fingerprint = quote_fingerprint(
            provider_call_id=provider_call.id,
            external_event_id=quote.event.external_event_id,
            player_external_ref=quote.player.as_external_ref(),
            stat_family=str(quote.stat_family),
            sportsbook=quote.sportsbook,
            line=quote.line,
            over_price=quote.over_price,
            under_price=quote.under_price,
            parser_version=self.parser_version,
        )
        already = self.session.execute(
            select(PropQuote.id).where(PropQuote.fingerprint == fingerprint)
        ).scalar_one_or_none()
        if already is not None:
            return None

        row = PropQuote(
            market_id=market_id,
            sportsbook=quote.sportsbook,
            line=quote.line,
            over_price=quote.over_price,
            under_price=quote.under_price,
            as_of_at=quote.as_of_at,
            retrieved_at=quote.retrieved_at,
            provider_market_updated_at=quote.provider_market_updated_at,
            source=quote.source,
            provider_call_id=provider_call.id,
            ingestion_run_id=run.id if run is not None else None,
            parser_version=self.parser_version,
            fingerprint=fingerprint,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def apply_outcome(self, run: IngestionRun, outcome: IngestionOutcome) -> None:
        run.quotes_observed += outcome.quotes_observed
        run.quotes_written += outcome.quotes_written
        run.quotes_deduplicated += outcome.quotes_deduplicated
        run.markets_quarantined += outcome.markets_quarantined
        self.session.flush()
