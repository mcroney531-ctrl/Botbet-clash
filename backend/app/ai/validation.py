"""Provider-neutral response validation. Never trust provider output: a
parsed_payload dict from any adapter (including MockAdapter) goes through
the exact same checks here before anything derived from it can be
persisted as a ForecastObservation.

This module handles *content* validation only (schema shape + market
coverage). Transport-level failures (timeout, auth, provider unavailable)
never reach here -- the orchestrator handles those directly from
ProviderCallResult.error.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from app.ai.prompts.versions import FORECAST_SCHEMA_VERSION
from app.ai.providers.base import ErrorCategory
from app.ai.schemas.benchmark_forecast import (
    BenchmarkForecastRequest,
    BenchmarkForecastResponse,
    ForecastItem,
)
from app.forecast_lab.market_math import PROBABILITY_PLACES

# Matches ForecastObservation.confidence's NUMERIC(4,2) column
# (app/db/models/forecast_lab.py) -- the single deterministic rounding
# policy for confidence, mirroring PROBABILITY_PLACES for probability.
CONFIDENCE_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    # Ordered (matches request.markets order) and quantized to storage
    # precision. None when is_valid is False.
    forecasts: list[ForecastItem] | None
    # JSON-safe dict form of `forecasts`, ready to persist verbatim as
    # AgentSession.validated_response. None when is_valid is False.
    validated_response: dict | None
    errors: list[str]
    error_category: ErrorCategory | None


def _invalid(errors: list[str]) -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        forecasts=None,
        validated_response=None,
        errors=errors,
        error_category="SCHEMA_VALIDATION_FAILED",
    )


def validate_benchmark_response(
    request: BenchmarkForecastRequest, parsed_payload: dict
) -> ValidationResult:
    """Validate a provider's already-JSON-parsed payload against `request`.

    Checks, in order: schema_version supported; pydantic shape (types,
    ranges, uncertainty enum, non-blank text, key_factors length); market
    coverage (no missing, no unknown, no duplicate market_id); then
    quantizes probability_over/confidence to storage precision.
    """

    if request.schema_version != FORECAST_SCHEMA_VERSION:
        return _invalid(
            [f"unsupported schema_version {request.schema_version!r}; expected {FORECAST_SCHEMA_VERSION!r}"]
        )

    try:
        response = BenchmarkForecastResponse.model_validate(parsed_payload)
    except ValidationError as exc:
        return _invalid([str(err) for err in exc.errors()])

    requested_ids = [m.market_id for m in request.markets]
    requested_id_set = set(requested_ids)

    seen: dict[str, int] = {}
    for item in response.forecasts:
        seen[item.market_id] = seen.get(item.market_id, 0) + 1

    duplicates = sorted(mid for mid, count in seen.items() if count > 1)
    unknown = sorted(mid for mid in seen if mid not in requested_id_set)
    missing = sorted(mid for mid in requested_id_set if mid not in seen)

    errors: list[str] = []
    if duplicates:
        errors.append(f"duplicate forecasts for market_id(s): {', '.join(duplicates)}")
    if unknown:
        errors.append(f"forecasts for unrequested market_id(s): {', '.join(unknown)}")
    if missing:
        errors.append(f"missing forecasts for market_id(s): {', '.join(missing)}")
    if errors:
        return _invalid(errors)

    by_market_id = {item.market_id: item for item in response.forecasts}
    ordered_quantized = [
        by_market_id[mid].model_copy(
            update={
                "probability_over": by_market_id[mid].probability_over.quantize(PROBABILITY_PLACES),
                "confidence": by_market_id[mid].confidence.quantize(CONFIDENCE_PLACES),
            }
        )
        for mid in requested_ids
    ]

    return ValidationResult(
        is_valid=True,
        forecasts=ordered_quantized,
        validated_response={"forecasts": [item.model_dump(mode="json") for item in ordered_quantized]},
        errors=[],
        error_category=None,
    )
