"""Provider-neutral adapter contract. Forecast Lab and the orchestrator's
business logic must never inspect a provider-native response object or
branch on provider name -- everything crosses this boundary as
ProviderCallResult / ProviderError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest

ErrorCategory = Literal[
    "AUTHENTICATION_ERROR",
    "RATE_LIMITED",
    "TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "INVALID_PROVIDER_RESPONSE",
    "SCHEMA_VALIDATION_FAILED",
    "CONTENT_REFUSAL",
    "UNKNOWN_PROVIDER_ERROR",
]

TRANSPORT_ERROR_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        "AUTHENTICATION_ERROR",
        "RATE_LIMITED",
        "TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "UNKNOWN_PROVIDER_ERROR",
    }
)
"""Categories that mean 'no usable answer at all' -- distinct from a
correction-retry-eligible failure, where the provider *did* respond but the
content failed validation (INVALID_PROVIDER_RESPONSE, SCHEMA_VALIDATION_FAILED,
CONTENT_REFUSAL)."""


@dataclass(frozen=True)
class ProviderError:
    category: ErrorCategory
    message: str
    provider_metadata: dict | None = None


@dataclass(frozen=True)
class ProviderCallResult:
    """Normalized result of a single provider call. Exactly one of
    (parsed_payload, error) should be meaningful: a successful call has a
    parsed_payload and no error; a failed call has an error and no
    parsed_payload. raw_response is preserved either way when available."""

    provider: str
    model_identifier: str
    raw_response: Any
    parsed_payload: dict | None
    provider_request_id: str | None
    usage_metadata: dict | None
    error: ProviderError | None


class CompetitorAdapter(Protocol):
    """One implementation per provider (openai / anthropic / google / mock).
    No provider-specific conditionals may exist outside these
    implementations -- the orchestrator resolves an adapter via the
    registry and calls only this interface."""

    provider_name: str

    def forecast_benchmark(self, request: BenchmarkForecastRequest) -> ProviderCallResult: ...


def error_result(
    *,
    provider: str,
    model_identifier: str,
    category: ErrorCategory,
    message: str,
    raw_response: Any = None,
    provider_request_id: str | None = None,
    usage_metadata: dict | None = None,
    provider_metadata: dict | None = None,
) -> ProviderCallResult:
    """Shared helper so every real adapter normalizes a failed call into
    ProviderCallResult the same way."""

    return ProviderCallResult(
        provider=provider,
        model_identifier=model_identifier,
        raw_response=raw_response,
        parsed_payload=None,
        provider_request_id=provider_request_id,
        usage_metadata=usage_metadata,
        error=ProviderError(category=category, message=message, provider_metadata=provider_metadata),
    )
