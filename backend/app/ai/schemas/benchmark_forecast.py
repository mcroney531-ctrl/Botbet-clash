"""BENCHMARK_FORECASTING request/response schemas (schema_version
"forecast-v1"). Provider-neutral: no adapter may add provider-specific
fields to these models, and Forecast Lab never sees anything but these
shapes (or the ORM rows built from them).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai.schemas.common import CallType, Uncertainty


class MarketContext(BaseModel):
    """Diagnostic market context, plus how complete it is.

    `books` counts the books actually CONSUMED. `books_observed` counts the
    books that had a quote at all, and the difference is what freshness
    refused. Without that pair, degraded coverage and naturally thin
    coverage are indistinguishable to a forecaster, so a feed problem
    reads as a market fact.

    Rejected prices are deliberately absent: a competitor learns the
    evidence was degraded, not what the degraded evidence said.
    """

    model_config = ConfigDict(extra="forbid")

    median_line: Decimal
    min_line: Decimal
    max_line: Decimal
    books: int = Field(ge=0)
    books_observed: int = Field(ge=0)
    stale_books_excluded: int = Field(ge=0)
    canonical_quote_stale: bool = False

    @model_validator(mode="after")
    def _coverage_adds_up(self) -> "MarketContext":
        if self.books_observed != self.books + self.stale_books_excluded:
            raise ValueError(
                "books_observed must equal books + stale_books_excluded; a book "
                "that falls out of both counts turns a dropped feed into a "
                "snapshot that merely looks thin"
            )
        return self


class MarketInput(BaseModel):
    """One benchmark slot's frozen, provider-neutral input. Everything here
    is derived from persisted immutable snapshots -- no live lookups."""

    model_config = ConfigDict(extra="forbid")

    market_id: str
    player: str
    team: str
    opponent: str
    stat_type: str
    canonical_line: Decimal
    canonical_over_price: int
    canonical_under_price: int
    canonical_market_probability_over: Decimal = Field(ge=0, le=1)
    same_line_consensus_probability_over: Decimal | None = Field(default=None, ge=0, le=1)
    market_context: MarketContext
    evidence: dict


class BenchmarkForecastRequest(BaseModel):
    """Sent to a single competitor's adapter. Identical eligible markets,
    evidence, and instructions must be produced for every competitor at a
    given checkpoint -- see AIOrchestrator / run_benchmark_round."""

    model_config = ConfigDict(extra="forbid")

    task_type: CallType = "BENCHMARK_FORECASTING"
    prompt_version: str
    schema_version: str
    season: int
    week: int
    checkpoint: str
    markets: list[MarketInput] = Field(min_length=1)

    @field_validator("markets")
    @classmethod
    def _unique_market_ids(cls, markets: list[MarketInput]) -> list[MarketInput]:
        ids = [m.market_id for m in markets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate market_id in request markets")
        return markets


class ForecastItem(BaseModel):
    """One competitor's forecast for one market, as returned by the
    provider (pre-quantization; see app/ai/validation.py for the single
    deterministic rounding policy applied before persistence)."""

    model_config = ConfigDict(extra="forbid")

    market_id: str
    probability_over: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=1, le=10)
    uncertainty: Uncertainty
    public_reasoning: str = Field(min_length=1)
    key_factors: list[str] = Field(max_length=5)
    primary_concern: str = Field(min_length=1)

    @field_validator("public_reasoning", "primary_concern")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("key_factors")
    @classmethod
    def _factors_not_blank(cls, factors: list[str]) -> list[str]:
        cleaned = [f.strip() for f in factors]
        if any(not f for f in cleaned):
            raise ValueError("key_factors entries must not be blank")
        return cleaned


class BenchmarkForecastResponse(BaseModel):
    """The provider's structured reply. Coverage against the request's
    markets (no missing, no unknown, no duplicates) is checked in
    app/ai/validation.py, not here -- this model only validates shape."""

    model_config = ConfigDict(extra="forbid")

    forecasts: list[ForecastItem] = Field(min_length=1)


def benchmark_response_json_schema() -> dict:
    """A plain JSON Schema (numbers as `number`, not the `Decimal` string
    encoding pydantic would otherwise emit) for real adapters that ask a
    provider to constrain its output to a schema (OpenAI structured
    outputs, Gemini `response_json_schema`, Anthropic `output_config`).
    Returns a fresh dict every call so a caller is free to mutate its copy
    (e.g. attaching provider-specific "strict" flags) without affecting
    others. This is intentionally hand-written rather than derived from
    the pydantic models above, whose Decimal fields would otherwise
    surface as schema type "string".

    Deliberately does NOT constrain numeric ranges (`minimum`/`maximum`)
    or array length (`maxItems`) here -- found live, against real
    Anthropic: its structured-outputs JSON Schema dialect is a stricter
    subset than OpenAI's/Gemini's and 400s on `minimum`/`maximum` for a
    `number` property ("output_config.format.schema: For 'number' type,
    properties maximum..."). These were never load-bearing anyway --
    app/ai/validation.py's pydantic model independently re-validates
    every returned value's range and every list's length regardless of
    what a provider's own schema enforces -- so dropping them keeps one
    genuinely shared schema across all three providers instead of
    special-casing Anthropic.
    """

    forecast_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "market_id",
            "probability_over",
            "confidence",
            "uncertainty",
            "public_reasoning",
            "key_factors",
            "primary_concern",
        ],
        "properties": {
            "market_id": {"type": "string"},
            "probability_over": {"type": "number"},
            "confidence": {"type": "number"},
            "uncertainty": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "public_reasoning": {"type": "string"},
            "key_factors": {"type": "array", "items": {"type": "string"}},
            "primary_concern": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["forecasts"],
        "properties": {"forecasts": {"type": "array", "items": forecast_item_schema, "minItems": 1}},
    }
