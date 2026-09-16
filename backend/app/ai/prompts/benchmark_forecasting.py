"""Prompt template for BENCHMARK_FORECASTING (prompt_version "benchmark-v2").

Neutral by design: every competitor gets the exact same system instructions.
Do not add per-provider or per-competitor personality here -- that
contaminates the research and belongs in the Show layer, later, on top of
these same underlying forecasts.

v2 (found live, against all three real providers at once): the first
paragraph below is unchanged -- it is the frozen research instruction --
but the output-field section is new. v1 relied on the request JSON
Schema's `minimum`/`maximum`/`maxItems` keywords to tell a model that
`confidence` is a 1-10 conviction scale rather than another 0-1
probability. Those keywords had to be dropped from the schema because
Anthropic's structured-outputs dialect rejects them (see
app/ai/schemas/benchmark_forecast.py), and with them went the only
signal carrying that meaning: OpenAI, Anthropic, and Gemini then all
independently returned confidence values of 0.7, 0.45, and 0.45.
Range/limit semantics belong in the instruction, not in a validation
keyword some providers silently don't support.
"""

from __future__ import annotations

from app.ai.prompts.versions import BENCHMARK_PROMPT_VERSION, FORECAST_SCHEMA_VERSION
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest

SYSTEM_INSTRUCTIONS = (
    "You are participating in a controlled NFL player-prop forecasting experiment. "
    "You are given a frozen evidence snapshot and contemporaneous betting-market "
    "information. Estimate the probability that each player's final official "
    "statistic will finish OVER the supplied line. Your probability should "
    "represent your own best calibrated estimate. Do not simply repeat the market "
    "probability unless you independently believe it is correct. Do not optimize "
    "for entertainment, disagreement, betting frequency, or confidence theater. "
    "It is acceptable for your estimate to remain close to the market. Return "
    "only the required structured response."
    "\n\n"
    "For every market in the request, return exactly one forecast object with "
    "these fields:\n"
    "- market_id: the market_id exactly as supplied. Return one forecast per "
    "requested market, no more and no fewer.\n"
    "- probability_over: a number from 0 to 1. The probability that the final "
    "official statistic is strictly greater than the supplied line.\n"
    "- confidence: a number from 1.0 to 10.0. This is a conviction scale, NOT a "
    "probability and NOT a 0-to-1 value: 1.0-4.9 very weak, 5.0-6.4 low, "
    "6.5-7.4 standard, 7.5-8.4 strong, 8.5-10.0 exceptional. A typical forecast "
    "sits near 7. Judge how much conviction you have in your own estimate for "
    "this specific market.\n"
    "- uncertainty: exactly one of LOW, MEDIUM, or HIGH.\n"
    "- public_reasoning: one to four sentences.\n"
    "- key_factors: at most five short items.\n"
    "- primary_concern: a single concise item."
)


def render_benchmark_prompt(request: BenchmarkForecastRequest) -> dict:
    """Build the exact, reproducible rendered request for one competitor.

    The returned dict is dual-purpose: adapters use it to build their
    provider-specific messages, and the orchestrator persists it verbatim
    as AgentSession.rendered_request -- storing the rendered request is
    simpler and safer than reconstructing it later from template + inputs.
    """

    if request.prompt_version != BENCHMARK_PROMPT_VERSION:
        raise ValueError(
            f"render_benchmark_prompt only supports prompt_version="
            f"{BENCHMARK_PROMPT_VERSION!r}, got {request.prompt_version!r}"
        )
    if request.schema_version != FORECAST_SCHEMA_VERSION:
        raise ValueError(
            f"render_benchmark_prompt only supports schema_version="
            f"{FORECAST_SCHEMA_VERSION!r}, got {request.schema_version!r}"
        )

    return {
        "prompt_version": request.prompt_version,
        "schema_version": request.schema_version,
        "system": SYSTEM_INSTRUCTIONS,
        "user": request.model_dump(mode="json"),
    }
