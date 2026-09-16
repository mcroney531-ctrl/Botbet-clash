"""Provider-neutral market-data contract (seam doc §8, §13).

Nothing above a provider adapter may inspect a vendor response object,
reference a vendor field name, or branch on provider identity. Everything
crosses this boundary as ProviderFetchResult / MarketDataError, exactly
as the Phase 3 model-provider layer does in app/ai/providers/base.py.

Two levels of failure exist here, and keeping them apart is the point:

  MarketDataError   the CALL failed -- no usable data at all
  ProviderDiagnostic  the call succeeded, but some slice of its CONTENT is
                    unusable (an unmapped market, a one-sided price, an
                    ambiguous alternate-line set)

Collapsing the second into the first would turn one bad market in a
40-market response into a total ingestion failure. Collapsing the first
into the second would let a 401 look like a quiet, empty week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Literal, Protocol, Sequence, TypeVar

from app.domain.enums import StatFamily
from app.marketdata.dto import ProviderEvent, ProviderEventRef, ProviderQuote

ErrorCategory = Literal[
    # Transport / access: no usable data at all.
    "AUTHENTICATION_ERROR",
    "QUOTA_EXHAUSTED",
    "QUOTA_RESERVE_EXHAUSTED",
    "RATE_LIMITED",
    "TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "UNKNOWN_PROVIDER_ERROR",
    # Content: the provider answered, the payload is unusable as a whole.
    "MALFORMED_RESPONSE",
]

DiagnosticCategory = Literal[
    # Per-market content problems. The call itself was fine.
    "UNSUPPORTED_MARKET",
    "AMBIGUOUS_ALTERNATE_LINE",
    "MISSING_CANONICAL_BOOK",
    "INCOMPLETE_PRICE_PAIR",
    "STALE_HISTORICAL_SNAPSHOT",
]

EndpointCapability = Literal["LIST_EVENTS", "FETCH_QUOTES", "FETCH_HISTORICAL"]

TRANSPORT_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "AUTHENTICATION_ERROR",
        "QUOTA_EXHAUSTED",
        "QUOTA_RESERVE_EXHAUSTED",
        "RATE_LIMITED",
        "TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "UNKNOWN_PROVIDER_ERROR",
    }
)
"""Categories meaning 'we never got an answer' -- as opposed to
MALFORMED_RESPONSE, where the provider did answer and we were billed
for it."""


@dataclass(frozen=True)
class MarketDataError:
    """A call-level failure.

    `message` MUST already be sanitized. It is persisted to
    provider_calls.error_message and printed in probe output, and this
    provider authenticates via a query parameter -- so an httpx transport
    exception stringifies to a URL containing the API key. Adapters
    normalize; they never pass str(exc) through.
    """

    category: ErrorCategory
    message: str


@dataclass(frozen=True)
class ProviderDiagnostic:
    """One piece of content we refused to normalize, and why.

    Carries enough to diagnose the refusal without carrying vendor JSON:
    which market key, which book, which player. These are counted into
    ingestion_runs.markets_quarantined and surfaced by the probe.
    """

    category: DiagnosticCategory
    detail: str
    vendor_market_key: str | None = None
    sportsbook: str | None = None
    player_display_name: str | None = None


@dataclass(frozen=True)
class ProviderCallMetadata:
    """Everything we record about one call attempt, including attempts
    that never produced an HTTP response (hence the nullability)."""

    endpoint_capability: EndpointCapability
    requested_at: datetime
    responded_at: datetime | None = None
    http_status: int | None = None
    quota_used: int | None = None
    quota_remaining: int | None = None
    quota_cost: int | None = None
    provider_request_id: str | None = None
    provider_snapshot_at: datetime | None = None
    raw_response_body: bytes | None = None
    raw_response_sha256: str | None = None
    raw_response_bytes: int | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderFetchResult(Generic[T]):
    """Exactly one of (payload, error) is meaningful at the call level.
    `call_metadata` is populated either way -- a failed call still cost
    quota and still belongs in the audit trail."""

    payload: T | None
    error: MarketDataError | None
    call_metadata: ProviderCallMetadata
    diagnostics: tuple[ProviderDiagnostic, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


class MarketDataProvider(Protocol):
    """One implementation per provider (the_odds_api / mock).

    Named for capabilities, not for a vendor's endpoint paths, so a
    differently-shaped provider can satisfy it without contortion.
    Callers pass INTERNAL StatFamily values; translating those into
    whatever the vendor calls them is the adapter's job and no one
    else's.
    """

    provider_name: str
    supports_historical: bool

    def list_events(
        self, *, sport: str, window_start: datetime, window_end: datetime
    ) -> ProviderFetchResult[list[ProviderEvent]]: ...

    def fetch_quotes(
        self,
        *,
        event: ProviderEventRef,
        stat_families: Sequence[StatFamily],
        books: Sequence[str] | None = None,
    ) -> ProviderFetchResult[list[ProviderQuote]]: ...

    def fetch_quotes_as_of(
        self,
        *,
        event: ProviderEventRef,
        stat_families: Sequence[StatFamily],
        as_of: datetime,
        books: Sequence[str] | None = None,
    ) -> ProviderFetchResult[list[ProviderQuote]]: ...


def error_result(
    *,
    category: ErrorCategory,
    message: str,
    call_metadata: ProviderCallMetadata,
    diagnostics: Sequence[ProviderDiagnostic] = (),
) -> ProviderFetchResult:
    return ProviderFetchResult(
        payload=None,
        error=MarketDataError(category=category, message=message),
        call_metadata=call_metadata,
        diagnostics=tuple(diagnostics),
    )
