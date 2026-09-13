"""Deterministic mock adapter. Proves orchestrator -> adapter -> validation
-> AgentSession -> ForecastObservation without any external network call,
and doubles as the failure-simulation harness (brief section on
MockAdapter failure simulations: malformed JSON, missing market, duplicate
market, out-of-range probability/confidence, invalid uncertainty, timeout,
provider error, and correction-retry sequences).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.ai.providers.base import ProviderCallResult, ProviderError
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest

OutcomeKind = Literal[
    "VALID",
    "MALFORMED_JSON",
    "MISSING_MARKET",
    "DUPLICATE_MARKET",
    "PROBABILITY_OUT_OF_RANGE",
    "INVALID_CONFIDENCE",
    "INVALID_UNCERTAINTY",
    "TIMEOUT",
    "PROVIDER_ERROR",
]


@dataclass(frozen=True)
class MockOutcome:
    """One scripted call outcome. Use the classmethods below rather than
    constructing directly -- they document what each simulation means."""

    kind: OutcomeKind

    @classmethod
    def valid(cls) -> "MockOutcome":
        return cls(kind="VALID")

    @classmethod
    def malformed_json(cls) -> "MockOutcome":
        return cls(kind="MALFORMED_JSON")

    @classmethod
    def missing_market(cls) -> "MockOutcome":
        return cls(kind="MISSING_MARKET")

    @classmethod
    def duplicate_market(cls) -> "MockOutcome":
        return cls(kind="DUPLICATE_MARKET")

    @classmethod
    def probability_out_of_range(cls) -> "MockOutcome":
        return cls(kind="PROBABILITY_OUT_OF_RANGE")

    @classmethod
    def invalid_confidence(cls) -> "MockOutcome":
        return cls(kind="INVALID_CONFIDENCE")

    @classmethod
    def invalid_uncertainty(cls) -> "MockOutcome":
        return cls(kind="INVALID_UNCERTAINTY")

    @classmethod
    def timeout(cls) -> "MockOutcome":
        return cls(kind="TIMEOUT")

    @classmethod
    def provider_error(cls) -> "MockOutcome":
        return cls(kind="PROVIDER_ERROR")


@dataclass
class MockAdapter:
    """provider_name is fixed to "mock" so the registry never confuses it
    with a real provider. fixtures maps market_id -> probability_over used
    to build VALID (and most malformed) responses deterministically.
    call_plan is consumed one entry per forecast_benchmark() call; once
    exhausted, every further call returns MockOutcome.valid()."""

    model_identifier: str
    fixtures: dict[str, float]
    call_plan: list[MockOutcome] = field(default_factory=list)
    provider_name: str = "mock"

    _calls_made: int = field(default=0, init=False, repr=False)

    def _next_outcome(self) -> MockOutcome:
        index = self._calls_made
        self._calls_made += 1
        if index < len(self.call_plan):
            return self.call_plan[index]
        return MockOutcome.valid()

    def _default_forecast(self, market_id: str) -> dict:
        probability = self.fixtures[market_id]
        return {
            "market_id": market_id,
            "probability_over": probability,
            "confidence": 6.5,
            "uncertainty": "MEDIUM",
            "public_reasoning": f"Mock deterministic forecast for {market_id}.",
            "key_factors": ["mock_factor"],
            "primary_concern": "mock_concern",
        }

    def forecast_benchmark(self, request: BenchmarkForecastRequest) -> ProviderCallResult:
        outcome = self._next_outcome()
        market_ids = [m.market_id for m in request.markets]

        if outcome.kind == "TIMEOUT":
            return ProviderCallResult(
                provider=self.provider_name,
                model_identifier=self.model_identifier,
                raw_response=None,
                parsed_payload=None,
                provider_request_id=None,
                usage_metadata=None,
                error=ProviderError(category="TIMEOUT", message="mock: simulated provider timeout"),
            )

        if outcome.kind == "PROVIDER_ERROR":
            return ProviderCallResult(
                provider=self.provider_name,
                model_identifier=self.model_identifier,
                raw_response=None,
                parsed_payload=None,
                provider_request_id=None,
                usage_metadata=None,
                error=ProviderError(
                    category="PROVIDER_UNAVAILABLE", message="mock: simulated provider unavailable"
                ),
            )

        if outcome.kind == "MALFORMED_JSON":
            raw = "{not valid json"
            return ProviderCallResult(
                provider=self.provider_name,
                model_identifier=self.model_identifier,
                raw_response=raw,
                parsed_payload=None,
                provider_request_id=f"mock-req-{index_suffix(self._calls_made)}",
                usage_metadata=None,
                error=ProviderError(
                    category="INVALID_PROVIDER_RESPONSE", message="mock: response was not valid JSON"
                ),
            )

        forecasts = [self._default_forecast(mid) for mid in market_ids]

        if outcome.kind == "MISSING_MARKET" and forecasts:
            forecasts = forecasts[:-1]
        elif outcome.kind == "DUPLICATE_MARKET" and forecasts:
            forecasts.append(dict(forecasts[0]))
        elif outcome.kind == "PROBABILITY_OUT_OF_RANGE" and forecasts:
            forecasts[0] = {**forecasts[0], "probability_over": 1.5}
        elif outcome.kind == "INVALID_CONFIDENCE" and forecasts:
            forecasts[0] = {**forecasts[0], "confidence": 42.0}
        elif outcome.kind == "INVALID_UNCERTAINTY" and forecasts:
            forecasts[0] = {**forecasts[0], "uncertainty": "SUPER_HIGH"}

        parsed_payload = {"forecasts": forecasts}
        return ProviderCallResult(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            raw_response=parsed_payload,
            parsed_payload=parsed_payload,
            provider_request_id=f"mock-req-{index_suffix(self._calls_made)}",
            usage_metadata={"mock": True},
            error=None,
        )


def index_suffix(n: int) -> str:
    return str(n).zfill(4)
