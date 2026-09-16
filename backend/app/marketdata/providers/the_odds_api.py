"""The Odds API v4 adapter.

THE ONLY module in this project permitted to know that The Odds API's
JSON exists or to construct its HTTP requests. Everything above it sees
normalized DTOs.

CREDENTIAL HANDLING -- read before editing
------------------------------------------
This provider authenticates with an `apiKey` QUERY PARAMETER, not a
header. That makes the request URL itself a secret, and it means the
ordinary Python reflex of surfacing `str(exc)` is a credential leak:
httpx embeds the full URL, query string included, in the text of its
transport exceptions. Every `except` clause here therefore constructs its
own fixed message and never interpolates the exception. The sentinel-key
regression test exists to keep it that way.

Two credential incidents already happened during Phase 3 setup. This is
the hardening that follows from them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

import httpx

from app.domain.enums import StatFamily
from app.marketdata.base import (
    EndpointCapability,
    ProviderCallMetadata,
    ProviderDiagnostic,
    ProviderFetchResult,
    ProviderShapeReport,
    error_result,
)
from app.marketdata.dto import ProviderEvent, ProviderEventRef, ProviderPlayerRef, ProviderQuote
from app.marketdata.mapping import TENTATIVE_MARKET_KEYS
from app.marketdata.telemetry import sha256_hex

PROVIDER_NAME = "THE_ODDS_API"
API_KEY_ENV_VAR = "THE_ODDS_API_KEY"
BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_SPORT = "americanfootball_nfl"
DEFAULT_REGION = "us"
ODDS_FORMAT = "american"
DEFAULT_TIMEOUT_SECONDS = 30.0
CANONICAL_BOOK = "DRAFTKINGS"

_FAMILY_TO_TENTATIVE_KEY = {family: key for key, family in TENTATIVE_MARKET_KEYS.items()}

_PLAYER_ID_FIELDS: tuple[str, ...] = ("player_id", "participant_id")
"""Fields whose NAME demonstrably means player/participant identity. Only
these may be reported as a stable player identifier."""

_AMBIGUOUS_ID_FIELDS: tuple[str, ...] = ("id", "participant", "outcome_id")
"""Identifier-ish fields whose semantics are NOT proven. A bare `id` could
key an outcome or a price record rather than a person, and treating it as a
player identity would silently merge or split real people in
Player.external_ref. Reported as unclassified, never as stable."""


class MissingCredentialError(RuntimeError):
    """Raised when the adapter is invoked without its key configured.

    Deliberately raised at INVOCATION, never at import or construction:
    the FastAPI app must boot and serve /health on a deployment where the
    key has not been set yet.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _quota_int(response: httpx.Response, header: str) -> int | None:
    raw = response.headers.get(header)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


class TheOddsApiProvider:
    """MarketDataProvider over The Odds API v4."""

    provider_name = PROVIDER_NAME
    supports_historical = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        region: str = DEFAULT_REGION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        verified_only: bool = True,
    ) -> None:
        self._api_key = api_key
        self.region = region
        self.timeout_seconds = timeout_seconds
        self._client = client
        # Production ingestion must run with verified_only=True. The probe
        # is the one caller allowed to pass False, because discovering the
        # real market keys is its entire job.
        self.verified_only = verified_only

    # -- credential -------------------------------------------------

    def _key(self) -> str:
        key = self._api_key or os.environ.get(API_KEY_ENV_VAR)
        if not key:
            raise MissingCredentialError(
                f"{API_KEY_ENV_VAR} is not set. Set it in the Railway service "
                "Variables UI for the backend service; never pass it on a "
                "command line or paste it into a chat."
            )
        return key

    # -- transport --------------------------------------------------

    def _request(
        self, *, capability: EndpointCapability, path: str, params: dict
    ) -> tuple[httpx.Response | None, ProviderCallMetadata, tuple[str, str] | None]:
        """Perform one call attempt.

        Returns (response, metadata, failure) where `failure` is a
        (category, message) pair built entirely from fixed strings. No
        branch in here interpolates an exception or a URL.
        """

        requested_at = _utcnow()
        base_meta = ProviderCallMetadata(endpoint_capability=capability, requested_at=requested_at)

        try:
            query = dict(params)
            query["apiKey"] = self._key()
        except MissingCredentialError as exc:
            # This message is ours and contains only the env var NAME.
            return None, base_meta, ("AUTHENTICATION_ERROR", str(exc))

        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        owns_client = self._client is None
        try:
            response = client.get(f"{BASE_URL}{path}", params=query)
        except httpx.TimeoutException:
            return None, base_meta, ("TIMEOUT", "request to the market-data provider timed out")
        except httpx.TransportError:
            # DNS, TLS, connection refused, reset. str(exc) would carry the
            # URL and therefore the key; it is never read.
            return None, base_meta, (
                "PROVIDER_UNAVAILABLE",
                "could not reach the market-data provider (transport failure)",
            )
        except Exception:
            return None, base_meta, (
                "UNKNOWN_PROVIDER_ERROR",
                "market-data provider call raised an unexpected client error",
            )
        finally:
            if owns_client:
                client.close()

        body = response.content
        meta = ProviderCallMetadata(
            endpoint_capability=capability,
            requested_at=requested_at,
            responded_at=_utcnow(),
            http_status=response.status_code,
            quota_used=_quota_int(response, "x-requests-used"),
            quota_remaining=_quota_int(response, "x-requests-remaining"),
            quota_cost=_quota_int(response, "x-requests-last"),
            provider_request_id=response.headers.get("x-request-id"),
            raw_response_body=body,
            raw_response_sha256=sha256_hex(body),
            raw_response_bytes=len(body),
        )

        if response.status_code in (401, 403):
            return response, meta, ("AUTHENTICATION_ERROR", "provider rejected the credential")
        if response.status_code == 429:
            return response, meta, ("RATE_LIMITED", "provider rate limit or quota reached")
        if response.status_code >= 500:
            return response, meta, (
                "PROVIDER_UNAVAILABLE",
                f"provider returned server error {response.status_code}",
            )
        if response.status_code >= 400:
            # The provider's own error text is not echoed: it can quote the
            # offending request back, URL included.
            return response, meta, (
                "MALFORMED_RESPONSE",
                f"provider rejected the request with status {response.status_code}",
            )
        return response, meta, None

    # -- MarketDataProvider ----------------------------------------

    def list_events(
        self, *, sport: str = DEFAULT_SPORT, window_start: datetime, window_end: datetime
    ) -> ProviderFetchResult[list[ProviderEvent]]:
        response, meta, failure = self._request(
            capability="LIST_EVENTS",
            path=f"/sports/{sport}/events",
            params={
                "commenceTimeFrom": _iso_z(window_start),
                "commenceTimeTo": _iso_z(window_end),
            },
        )
        if failure is not None:
            return error_result(category=failure[0], message=failure[1], call_metadata=meta)

        try:
            raw = response.json()
        except ValueError:
            return error_result(
                category="MALFORMED_RESPONSE",
                message="events response was not valid JSON",
                call_metadata=meta,
            )
        if not isinstance(raw, list):
            return error_result(
                category="MALFORMED_RESPONSE",
                message="events response was not a JSON array",
                call_metadata=meta,
            )

        events: list[ProviderEvent] = []
        for item in raw:
            try:
                events.append(
                    ProviderEvent(
                        ref=ProviderEventRef(
                            provider=self.provider_name, external_event_id=str(item["id"])
                        ),
                        sport_key=str(item.get("sport_key", sport)),
                        kickoff_at=_parse_iso(item["commence_time"]),
                        home_team=str(item["home_team"]),
                        away_team=str(item["away_team"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # One malformed event does not invalidate the response.
                continue
        return ProviderFetchResult(payload=events, error=None, call_metadata=meta)

    def fetch_quotes(self, *, event, stat_families, books=None):
        raise NotImplementedError(
            "Quote normalization is deliberately not implemented in Phase 4A.1. "
            "The vendor's payload shape for player props -- stable player id, "
            "team/position availability, alternate-line structure, and the level "
            "at which last_update is reported -- are all OPEN questions that the "
            "validation probe must answer from a real response first. Writing a "
            "parser against assumptions now is exactly what the seam exists to "
            "prevent. See backend/docs/phase4-ingestion-seam.md §18."
        )

    def fetch_quotes_as_of(self, *, event, stat_families, as_of, books=None):
        raise NotImplementedError(
            "Historical ingestion is Phase 4B and requires the paid tier. "
            "See backend/docs/phase4-ingestion-seam.md §14."
        )

    # -- shape discovery (probe only) ------------------------------

    def discover_event_shape(
        self,
        *,
        event: ProviderEventRef,
        stat_families: Sequence[StatFamily],
        sport: str = DEFAULT_SPORT,
        now: datetime | None = None,
    ) -> tuple[list[ProviderFetchResult], ProviderShapeReport]:
        """Learn this vendor's player-prop payload shape, one family at a time.

        Every vendor field name in the project lives in this module and
        nowhere else. The probe CLI consumes the neutral
        ProviderShapeReport and never indexes into vendor JSON -- that
        separation is the entire point of the seam, and it applies to
        diagnostic code just as much as to the ingestion path.

        One call per family: the per-market-per-region credit cost is the
        same either way, but a combined request that rejects one bad key
        tells us nothing about WHICH spelling was wrong.

        Returns EVERY call's result, not just the last, so the caller can
        record complete per-response quota telemetry.
        """

        observed = now or _utcnow()
        report = _ShapeAccumulator(canonical_book=CANONICAL_BOOK)
        results: list[ProviderFetchResult] = []

        for family in stat_families:
            vendor_key = _vendor_key_for(family)
            response, meta, failure = self._request(
                capability="FETCH_QUOTES",
                path=f"/sports/{sport}/events/{event.external_event_id}/odds",
                params={
                    "regions": self.region,
                    "markets": vendor_key,
                    "oddsFormat": ODDS_FORMAT,
                },
            )
            if failure is not None:
                results.append(
                    error_result(category=failure[0], message=failure[1], call_metadata=meta)
                )
                report.note_failure(vendor_key, failure[0], failure[1])
                continue
            try:
                decoded = response.json()
            except ValueError:
                results.append(
                    error_result(
                        category="MALFORMED_RESPONSE",
                        message="event-odds response was not valid JSON",
                        call_metadata=meta,
                    )
                )
                report.note_failure(vendor_key, "MALFORMED_RESPONSE", "not valid JSON")
                continue

            results.append(ProviderFetchResult(payload=None, error=None, call_metadata=meta))
            self._absorb_event_odds(decoded, report, family, vendor_key, event, observed)

        return results, report.build()

    def _absorb_event_odds(
        self,
        decoded: Any,
        report: "_ShapeAccumulator",
        family: StatFamily,
        vendor_key: str,
        event: ProviderEventRef,
        observed: datetime,
    ) -> None:
        """The ONLY place vendor JSON keys are read."""

        if not isinstance(decoded, dict):
            report.note_failure(vendor_key, "MALFORMED_RESPONSE", "payload was not an object")
            return

        for bookmaker in decoded.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            book = str(bookmaker.get("key", "")).upper()
            report.note_book(book)
            book_last_update = bookmaker.get("last_update")
            if book_last_update is not None:
                report.note_last_update_level("BOOKMAKER")

            for market in bookmaker.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                market_key = str(market.get("key", ""))
                report.note_market_key(market_key)
                market_last_update = market.get("last_update")
                if market_last_update is not None:
                    report.note_last_update_level("MARKET")
                    report.note_market_last_update(str(market_last_update))

                outcomes = [o for o in (market.get("outcomes") or []) if isinstance(o, dict)]
                if outcomes and book == CANONICAL_BOOK:
                    report.note_canonical_family(family)

                # Group by (player, line) so Over and Under can be matched.
                by_player_line: dict[tuple[str, str], dict[str, dict]] = {}
                lines_per_player: dict[str, set[str]] = {}
                for outcome in outcomes:
                    who = str(outcome.get("description", "") or "")
                    if who:
                        report.note_player_name_field("description")
                    for candidate in _PLAYER_ID_FIELDS:
                        if outcome.get(candidate) is not None:
                            report.note_player_identifier(candidate, verified=True)
                            break
                    else:
                        for candidate in _AMBIGUOUS_ID_FIELDS:
                            if outcome.get(candidate) is not None:
                                report.note_player_identifier(candidate, verified=False)
                                break
                    if outcome.get("team") is not None:
                        report.note_team_available()
                    if outcome.get("position") is not None:
                        report.note_position_available()

                    point = outcome.get("point")
                    side = str(outcome.get("name", "") or "").strip().upper()
                    if not who or point is None or side not in ("OVER", "UNDER"):
                        continue
                    lines_per_player.setdefault(who, set()).add(str(point))
                    by_player_line.setdefault((who, str(point)), {})[side] = outcome

                for who, lines in lines_per_player.items():
                    if len(lines) > 1:
                        report.note_multiple_lines(who, sorted(lines))

                if report.has_sample or book != CANONICAL_BOOK:
                    continue
                for (who, point), sides in by_player_line.items():
                    over, under = sides.get("OVER"), sides.get("UNDER")
                    if over is None or under is None:
                        report.note_unpaired(book, who, point)
                        continue
                    try:
                        report.set_sample(
                            ProviderQuote(
                                event=event,
                                player=ProviderPlayerRef(
                                    provider=self.provider_name,
                                    display_name=who,
                                    external_player_id=_first_present(over, _PLAYER_ID_FIELDS),
                                    team=over.get("team"),
                                    position=over.get("position"),
                                ),
                                stat_family=family,
                                vendor_market_key=market_key,
                                sportsbook=book,
                                line=Decimal(str(point)),
                                over_price=int(over["price"]),
                                under_price=int(under["price"]),
                                as_of_at=observed,
                                retrieved_at=observed,
                                source=self.provider_name,
                                provider_market_updated_at=(
                                    _parse_iso(market_last_update)
                                    if market_last_update is not None
                                    else None
                                ),
                            )
                        )
                    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                        report.note_sample_failure(f"{type(exc).__name__} building the DTO")
                    break


def _first_present(outcome: dict, fields: Sequence[str]) -> str | None:
    for field_name in fields:
        value = outcome.get(field_name)
        if value is not None:
            return str(value)
    return None


def _vendor_key_for(family: StatFamily) -> str:
    return _FAMILY_TO_TENTATIVE_KEY[family]


def _iso_z(value: datetime) -> str:
    """The Odds API wants second-precision ISO8601 with a literal Z."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class _ShapeAccumulator:
    """Collects observations while walking one vendor payload.

    Mutable on purpose; `build()` freezes it into the neutral
    ProviderShapeReport that crosses the boundary.
    """

    canonical_book: str
    market_keys: list[str] = field(default_factory=list)
    books: list[str] = field(default_factory=list)
    canonical_families: list[str] = field(default_factory=list)
    last_update_levels: list[str] = field(default_factory=list)
    market_last_update: str | None = None
    identifier_field: str | None = None
    identifier_verified: bool = False
    name_field_seen: bool = False
    team_seen: bool = False
    position_seen: bool = False
    alternate_note: str | None = None
    sample: ProviderQuote | None = None
    sample_failure: str | None = None
    unpaired: list[str] = field(default_factory=list)
    diagnostics: list[ProviderDiagnostic] = field(default_factory=list)

    @property
    def has_sample(self) -> bool:
        return self.sample is not None

    def note_book(self, book: str) -> None:
        if book and book not in self.books:
            self.books.append(book)

    def note_market_key(self, key: str) -> None:
        if key and key not in self.market_keys:
            self.market_keys.append(key)

    def note_canonical_family(self, family: StatFamily) -> None:
        if family.value not in self.canonical_families:
            self.canonical_families.append(family.value)

    def note_last_update_level(self, level: str) -> None:
        if level not in self.last_update_levels:
            self.last_update_levels.append(level)

    def note_market_last_update(self, value: str) -> None:
        if self.market_last_update is None:
            self.market_last_update = value

    def note_player_name_field(self, field_name: str) -> None:
        self.name_field_seen = True

    def note_player_identifier(self, field_name: str, *, verified: bool) -> None:
        # A verified identifier always wins over an ambiguous one already seen.
        if verified and not self.identifier_verified:
            self.identifier_field, self.identifier_verified = field_name, True
        elif self.identifier_field is None:
            self.identifier_field, self.identifier_verified = field_name, False

    def note_team_available(self) -> None:
        self.team_seen = True

    def note_position_available(self) -> None:
        self.position_seen = True

    def note_multiple_lines(self, player: str, lines: Sequence[str]) -> None:
        if self.alternate_note is None:
            self.alternate_note = (
                f"MULTIPLE_LINES_WITHIN_ONE_MARKET_KEY ({player}: {', '.join(lines)})"
            )

    def note_unpaired(self, book: str, player: str, point: str) -> None:
        entry = f"{book}/{player}@{point}"
        if entry not in self.unpaired:
            self.unpaired.append(entry)
            self.diagnostics.append(
                ProviderDiagnostic(
                    category="INCOMPLETE_PRICE_PAIR",
                    detail=f"only one side present at line {point}",
                    sportsbook=book,
                    player_display_name=player,
                )
            )

    def set_sample(self, quote: ProviderQuote) -> None:
        if self.sample is None:
            self.sample = quote

    def note_sample_failure(self, reason: str) -> None:
        if self.sample_failure is None:
            self.sample_failure = reason

    def note_failure(self, vendor_key: str, category: str, detail: str) -> None:
        self.diagnostics.append(
            ProviderDiagnostic(
                # A family we could not read is quarantined, never coerced --
                # the same rule the ingestion path applies to an unknown key.
                category="UNSUPPORTED_MARKET",
                detail=f"{category}: {detail}",
                vendor_market_key=vendor_key,
            )
        )

    def _identity(self) -> tuple[str, str | None, bool]:
        if self.identifier_field and self.identifier_verified:
            return f"STABLE_PLAYER_ID (field={self.identifier_field})", self.identifier_field, True
        if self.identifier_field:
            return (
                f"UNCLASSIFIED_IDENTIFIER (field={self.identifier_field}) — present but its "
                "semantics are NOT proven to be player identity; treated as no stable id",
                self.identifier_field,
                False,
            )
        if self.name_field_seen:
            return "DISPLAY_NAME_ONLY", None, False
        return "UNDETERMINED", None, False

    def _alternate_shape(self) -> str:
        if self.alternate_note:
            return self.alternate_note
        if self.market_keys:
            if any(k.endswith("_alternate") for k in self.market_keys):
                return "SEPARATE_ALTERNATE_MARKET_KEYS (observed)"
            return "ONE_LINE_PER_PLAYER_IN_EACH_MAPPED_MARKET_KEY"
        return "UNDETERMINED"

    def _sample_reason(self) -> str | None:
        if self.sample is not None:
            return None
        if self.sample_failure:
            return self.sample_failure
        if self.unpaired:
            return (
                f"no matched Over/Under pair at the canonical book; unpaired sides seen: "
                f"{', '.join(self.unpaired[:3])}"
            )
        return f"no {self.canonical_book} outcomes were returned for any requested family"

    def build(self) -> ProviderShapeReport:
        identity, identifier_field, verified = self._identity()
        return ProviderShapeReport(
            market_keys_returned=tuple(self.market_keys),
            books_seen=tuple(self.books),
            canonical_book_present=self.canonical_book in self.books,
            families_quoted_by_canonical=tuple(self.canonical_families),
            player_identity=identity,
            player_identifier_field=identifier_field,
            player_identity_verified=verified,
            team_available=self.team_seen,
            position_available=self.position_seen,
            alternate_line_shape=self._alternate_shape(),
            last_update_levels=tuple(self.last_update_levels),
            market_last_update_sample=self.market_last_update,
            sample_quote=self.sample,
            sample_quote_unavailable_reason=self._sample_reason(),
            diagnostics=tuple(self.diagnostics),
        )
