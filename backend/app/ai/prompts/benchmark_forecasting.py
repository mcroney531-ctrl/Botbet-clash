"""Prompt template for BENCHMARK_FORECASTING (prompt_version "benchmark-v1").

Neutral by design: every competitor gets the exact same system instructions.
Do not add per-provider or per-competitor personality here -- that
contaminates the research and belongs in the Show layer, later, on top of
these same underlying forecasts.
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
