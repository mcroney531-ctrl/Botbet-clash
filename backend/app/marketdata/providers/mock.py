"""Deterministic fixture provider.

Exists so the whole pipeline -- normalization, fingerprinting, dedup,
quarantine, partial success, every failure category -- can be proven
without spending a single provider credit. Nothing here touches the
network.

Scenarios are selected explicitly rather than inferred, so a test says
what it is testing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from app.domain.enums import StatFamily
from app.marketdata.base import (
    ProviderCallMetadata,
    ProviderDiagnostic,
    ProviderFetchResult,
    error_result,
)
from app.marketdata.dto import ProviderEvent, ProviderEventRef, ProviderPlayerRef, ProviderQuote
from app.marketdata.telemetry import sha256_hex

MOCK_PROVIDER_NAME = "MOCK_MARKET_DATA"

SCENARIOS = (
    "STANDARD",              # DK + two comparison books, one clean market
    "LINE_MOVED",            # same market, different prices
    "MISSING_CANONICAL",     # comparison books only, no DraftKings
    "UNKNOWN_MARKET",        # a vendor key we do not map
    "INCOMPLETE_PAIR",       # Over with no Under
    "ALTERNATE_LINES",       # two candidate lines for one book/player/stat
    "MALFORMED",             # provider answered with unparseable content
    "AUTH_FAILURE",
    "QUOTA_EXHAUSTED",
    "TRANSPORT_FAILURE",
    "RATE_LIMITED",
    "TIMEOUT",
)


def _meta(capability, *, body: bytes | None = None, cost: int = 5, **kw) -> ProviderCallMetadata:
    now = kw.pop("now", None) or datetime.now(timezone.utc)
    return ProviderCallMetadata(
        endpoint_capability=capability,
        requested_at=now,
        responded_at=kw.pop("responded_at", now),
        http_status=kw.pop("http_status", 200),
        quota_used=kw.pop("quota_used", 100),
        quota_remaining=kw.pop("quota_remaining", 9900),
        quota_cost=cost,
        provider_request_id=kw.pop("provider_request_id", "mock-request-1"),
        provider_snapshot_at=kw.pop("provider_snapshot_at", None),
        raw_response_body=body,
        raw_response_sha256=sha256_hex(body) if body is not None else None,
        raw_response_bytes=len(body) if body is not None else None,
    )


class MockMarketDataProvider:
    """A MarketDataProvider whose behaviour is chosen, not discovered."""

    provider_name = MOCK_PROVIDER_NAME
    supports_historical = True

    def __init__(
        self,
        *,
        scenario: str = "STANDARD",
        now: datetime | None = None,
        quota_remaining: int = 9900,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
        self.scenario = scenario
        self.now = now or datetime.now(timezone.utc)
        self.quota_remaining = quota_remaining
        self.call_count = 0

    # -- helpers ----------------------------------------------------

    def _event_ref(self) -> ProviderEventRef:
        return ProviderEventRef(provider=self.provider_name, external_event_id="mock-event-1")

    def _player(self, *, with_id: bool = True, team: str | None = "KC") -> ProviderPlayerRef:
        return ProviderPlayerRef(
            provider=self.provider_name,
            display_name="Mock Receiver",
            external_player_id="mock-player-1" if with_id else None,
            team=team,
            position="WR" if team else None,
        )

    def _quote(
        self,
        *,
        book: str,
        line: str,
        over: int,
        under: int,
        as_of: datetime | None = None,
        family: StatFamily = StatFamily.RECEIVING_YARDS,
        vendor_key: str = "player_reception_yds",
    ) -> ProviderQuote:
        observed = as_of or self.now
        return ProviderQuote(
            event=self._event_ref(),
            player=self._player(),
            stat_family=family,
            vendor_market_key=vendor_key,
            sportsbook=book,
            line=Decimal(line),
            over_price=over,
            under_price=under,
            as_of_at=observed,
            retrieved_at=max(observed, self.now),
            source=self.provider_name,
            provider_market_updated_at=observed - timedelta(minutes=8),
        )

    def _failure(self, capability):
        table = {
            "AUTH_FAILURE": ("AUTHENTICATION_ERROR", "provider rejected the credential", 401),
            "QUOTA_EXHAUSTED": ("QUOTA_EXHAUSTED", "monthly credit allowance is spent", 429),
            "RATE_LIMITED": ("RATE_LIMITED", "too many requests", 429),
            "TIMEOUT": ("TIMEOUT", "request timed out", None),
            "TRANSPORT_FAILURE": ("PROVIDER_UNAVAILABLE", "connection failed", None),
            "MALFORMED": ("MALFORMED_RESPONSE", "response was not valid JSON", 200),
        }
        if self.scenario not in table:
            return None
        category, message, status = table[self.scenario]
        return error_result(
            category=category,
            message=message,
            call_metadata=_meta(
                capability,
                now=self.now,
                http_status=status,
                quota_remaining=self.quota_remaining,
                # A failed call can still have cost quota -- that is exactly
                # why telemetry is recorded for failures too.
                cost=0 if status is None else 1,
                body=b"<<not json>>" if self.scenario == "MALFORMED" else None,
            ),
        )

    # -- MarketDataProvider ----------------------------------------

    def list_events(self, *, sport, window_start, window_end):
        self.call_count += 1
        failure = self._failure("LIST_EVENTS")
        if failure is not None:
            return failure
        events = [
            ProviderEvent(
                ref=self._event_ref(),
                sport_key=sport,
                kickoff_at=self.now + timedelta(days=3),
                home_team="KC",
                away_team="SF",
            )
        ]
        body = json.dumps([{"id": "mock-event-1"}]).encode()
        return ProviderFetchResult(
            payload=events,
            error=None,
            call_metadata=_meta(
                "LIST_EVENTS", body=body, cost=0, now=self.now,
                quota_remaining=self.quota_remaining,
            ),
        )

    def fetch_quotes(self, *, event, stat_families, books=None):
        self.call_count += 1
        return self._quotes("FETCH_QUOTES", as_of=self.now, snapshot_at=None)

    def fetch_quotes_as_of(self, *, event, stat_families, as_of, books=None):
        self.call_count += 1
        # Historical: as_of_at is the provider's returned snapshot time,
        # never our clock -- and retrieved_at is genuinely later.
        return self._quotes("FETCH_HISTORICAL", as_of=as_of, snapshot_at=as_of)

    def _quotes(self, capability, *, as_of: datetime, snapshot_at: datetime | None):
        failure = self._failure(capability)
        if failure is not None:
            return failure

        diagnostics: list[ProviderDiagnostic] = []
        quotes: list[ProviderQuote] = []

        if self.scenario == "MISSING_CANONICAL":
            quotes = [
                self._quote(book="FANDUEL", line="74.5", over=-110, under=-110, as_of=as_of),
                self._quote(book="BETMGM", line="75.5", over=-105, under=-115, as_of=as_of),
            ]
            diagnostics.append(
                ProviderDiagnostic(
                    category="MISSING_CANONICAL_BOOK",
                    detail="DRAFTKINGS absent for this market",
                    vendor_market_key="player_reception_yds",
                    player_display_name="Mock Receiver",
                )
            )
        elif self.scenario == "UNKNOWN_MARKET":
            quotes = [self._quote(book="DRAFTKINGS", line="74.5", over=-115, under=-105, as_of=as_of)]
            diagnostics.append(
                ProviderDiagnostic(
                    category="UNSUPPORTED_MARKET",
                    detail="vendor key is not in the mapping table; quarantined, not coerced",
                    vendor_market_key="player_pass_attempts",
                )
            )
        elif self.scenario == "INCOMPLETE_PAIR":
            quotes = [self._quote(book="FANDUEL", line="74.5", over=-110, under=-110, as_of=as_of)]
            diagnostics.append(
                ProviderDiagnostic(
                    category="INCOMPLETE_PRICE_PAIR",
                    detail="DRAFTKINGS returned Over with no matching Under at 74.5",
                    vendor_market_key="player_reception_yds",
                    sportsbook="DRAFTKINGS",
                    player_display_name="Mock Receiver",
                )
            )
        elif self.scenario == "ALTERNATE_LINES":
            # Two candidate lines for the SAME book/player/stat. The
            # ingestion service must refuse both, not choose.
            quotes = [
                self._quote(book="DRAFTKINGS", line="74.5", over=-115, under=-105, as_of=as_of),
                self._quote(book="DRAFTKINGS", line="79.5", over=+130, under=-160, as_of=as_of),
                self._quote(book="FANDUEL", line="74.5", over=-110, under=-110, as_of=as_of),
            ]
        elif self.scenario == "LINE_MOVED":
            quotes = [
                self._quote(book="DRAFTKINGS", line="76.5", over=-108, under=-112, as_of=as_of),
                self._quote(book="FANDUEL", line="76.5", over=-110, under=-110, as_of=as_of),
            ]
        else:  # STANDARD
            quotes = [
                self._quote(book="DRAFTKINGS", line="74.5", over=-115, under=-105, as_of=as_of),
                self._quote(book="FANDUEL", line="74.5", over=-110, under=-110, as_of=as_of),
                self._quote(book="BETMGM", line="75.5", over=-105, under=-115, as_of=as_of),
            ]

        body = json.dumps(
            [
                {
                    "bookmaker": q.sportsbook,
                    "line": str(q.line),
                    "over": q.over_price,
                    "under": q.under_price,
                }
                for q in quotes
            ]
        ).encode()
        return ProviderFetchResult(
            payload=quotes,
            error=None,
            call_metadata=_meta(
                capability,
                body=body,
                now=self.now,
                cost=5,
                quota_remaining=self.quota_remaining,
                provider_snapshot_at=snapshot_at,
            ),
            diagnostics=tuple(diagnostics),
        )
