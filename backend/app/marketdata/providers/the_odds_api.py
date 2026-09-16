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
from datetime import datetime, timezone
from typing import Sequence

import httpx

from app.domain.enums import StatFamily
from app.marketdata.base import (
    EndpointCapability,
    ProviderCallMetadata,
    ProviderFetchResult,
    error_result,
)
from app.marketdata.dto import ProviderEvent, ProviderEventRef
from app.marketdata.telemetry import sha256_hex

PROVIDER_NAME = "THE_ODDS_API"
API_KEY_ENV_VAR = "THE_ODDS_API_KEY"
BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_SPORT = "americanfootball_nfl"
DEFAULT_REGION = "us"
ODDS_FORMAT = "american"
DEFAULT_TIMEOUT_SECONDS = 30.0


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

    # -- probe support ---------------------------------------------

    def raw_event_odds(
        self, *, event_id: str, market_keys: Sequence[str], sport: str = DEFAULT_SPORT
    ) -> tuple[ProviderFetchResult, object | None]:
        """Fetch one event's odds and hand back the DECODED payload.

        Used only by the validation probe, whose entire purpose is to
        inspect a shape we have not yet committed to parsing. It returns
        the raw structure alongside the normal result so the probe can
        report what the vendor actually sends. Nothing in the ingestion
        path may call this -- that would be the vendor JSON leaking
        upward, which the seam forbids.
        """

        response, meta, failure = self._request(
            capability="FETCH_QUOTES",
            path=f"/sports/{sport}/events/{event_id}/odds",
            params={
                "regions": self.region,
                "markets": ",".join(market_keys),
                "oddsFormat": ODDS_FORMAT,
            },
        )
        if failure is not None:
            return error_result(category=failure[0], message=failure[1], call_metadata=meta), None
        try:
            decoded = response.json()
        except ValueError:
            return (
                error_result(
                    category="MALFORMED_RESPONSE",
                    message="event-odds response was not valid JSON",
                    call_metadata=meta,
                ),
                None,
            )
        return ProviderFetchResult(payload=decoded, error=None, call_metadata=meta), decoded


def _iso_z(value: datetime) -> str:
    """The Odds API wants second-precision ISO8601 with a literal Z."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
