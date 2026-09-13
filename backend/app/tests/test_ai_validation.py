"""Pure (no-DB) tests for app/ai/validation.py and MockAdapter's failure
simulations -- one test per failure mode listed in the Phase 3 brief.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.prompts.versions import BENCHMARK_PROMPT_VERSION, FORECAST_SCHEMA_VERSION
from app.ai.providers.mock import MockAdapter, MockOutcome
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, ForecastItem
from app.ai.validation import CONFIDENCE_PLACES, validate_benchmark_response
from app.forecast_lab.market_math import PROBABILITY_PLACES


def make_request(market_ids: list[str]) -> BenchmarkForecastRequest:
    return BenchmarkForecastRequest(
        prompt_version=BENCHMARK_PROMPT_VERSION,
        schema_version=FORECAST_SCHEMA_VERSION,
        season=2026,
        week=1,
        checkpoint="OPENING",
        markets=[
            {
                "market_id": mid,
                "player": "P",
                "team": "A",
                "opponent": "B",
                "stat_type": "RECEIVING_YARDS",
                "canonical_line": "74.5",
                "canonical_over_price": -115,
                "canonical_under_price": -105,
                "canonical_market_probability_over": "0.51",
                "same_line_consensus_probability_over": None,
                "market_context": {"median_line": "74.5", "min_line": "73.5", "max_line": "75.5", "books": 6},
                "evidence": {},
            }
            for mid in market_ids
        ],
    )


def run_mock(request: BenchmarkForecastRequest, fixtures: dict[str, float], outcome: MockOutcome):
    adapter = MockAdapter(model_identifier="mock-1", fixtures=fixtures, call_plan=[outcome])
    return adapter.forecast_benchmark(request)


def test_valid_response_validates_and_quantizes():
    request = make_request(["m1", "m2"])
    result = run_mock(request, {"m1": 0.6123456, "m2": 0.4499}, MockOutcome.valid())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is True
    assert validation.errors == []
    assert [f.market_id for f in validation.forecasts] == ["m1", "m2"]  # ordered per request
    assert validation.forecasts[0].probability_over == Decimal("0.6123456").quantize(PROBABILITY_PLACES)
    assert validation.forecasts[0].confidence == Decimal("6.5").quantize(CONFIDENCE_PLACES)


def test_malformed_json_never_reaches_validation():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.malformed_json())

    assert result.error is not None
    assert result.error.category == "INVALID_PROVIDER_RESPONSE"
    assert result.parsed_payload is None


def test_missing_market_is_rejected():
    request = make_request(["m1", "m2"])
    result = run_mock(request, {"m1": 0.6, "m2": 0.5}, MockOutcome.missing_market())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is False
    assert validation.error_category == "SCHEMA_VALIDATION_FAILED"
    assert any("missing forecasts" in e for e in validation.errors)


def test_duplicate_market_is_rejected():
    request = make_request(["m1", "m2"])
    result = run_mock(request, {"m1": 0.6, "m2": 0.5}, MockOutcome.duplicate_market())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is False
    assert any("duplicate forecasts" in e for e in validation.errors)


def test_unknown_market_is_rejected():
    request = make_request(["m1"])
    payload = {
        "forecasts": [
            {
                "market_id": "m1",
                "probability_over": 0.6,
                "confidence": 6.5,
                "uncertainty": "MEDIUM",
                "public_reasoning": "x",
                "key_factors": ["a"],
                "primary_concern": "b",
            },
            {
                "market_id": "not-requested",
                "probability_over": 0.6,
                "confidence": 6.5,
                "uncertainty": "MEDIUM",
                "public_reasoning": "x",
                "key_factors": ["a"],
                "primary_concern": "b",
            },
        ]
    }
    validation = validate_benchmark_response(request, payload)

    assert validation.is_valid is False
    assert any("unrequested" in e for e in validation.errors)


def test_probability_out_of_range_is_rejected():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.probability_out_of_range())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is False
    assert validation.error_category == "SCHEMA_VALIDATION_FAILED"


def test_invalid_confidence_is_rejected():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.invalid_confidence())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is False


def test_invalid_uncertainty_is_rejected():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.invalid_uncertainty())
    validation = validate_benchmark_response(request, result.parsed_payload)

    assert validation.is_valid is False


def test_provider_timeout_is_a_transport_error_not_a_validation_failure():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.timeout())

    assert result.error is not None
    assert result.error.category == "TIMEOUT"
    assert result.parsed_payload is None


def test_provider_unavailable_is_a_transport_error():
    request = make_request(["m1"])
    result = run_mock(request, {"m1": 0.6}, MockOutcome.provider_error())

    assert result.error is not None
    assert result.error.category == "PROVIDER_UNAVAILABLE"


def test_unsupported_schema_version_is_rejected_before_shape_checks():
    request = make_request(["m1"])
    object.__setattr__(request, "schema_version", "forecast-v999")
    validation = validate_benchmark_response(request, {"forecasts": []})

    assert validation.is_valid is False
    assert "unsupported schema_version" in validation.errors[0]


def test_key_factors_over_five_are_rejected_at_the_schema_layer():
    with pytest.raises(ValidationError):
        ForecastItem(
            market_id="m1",
            probability_over=Decimal("0.5"),
            confidence=Decimal("6.5"),
            uncertainty="MEDIUM",
            public_reasoning="x",
            key_factors=["a", "b", "c", "d", "e", "f"],
            primary_concern="y",
        )


def test_blank_public_reasoning_is_rejected_at_the_schema_layer():
    with pytest.raises(ValidationError):
        ForecastItem(
            market_id="m1",
            probability_over=Decimal("0.5"),
            confidence=Decimal("6.5"),
            uncertainty="MEDIUM",
            public_reasoning="   ",
            key_factors=["a"],
            primary_concern="y",
        )
