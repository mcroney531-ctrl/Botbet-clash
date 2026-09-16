"""Frozen version identifiers. Bump only when the corresponding template
or schema actually changes -- these are persisted on every AgentSession
and are part of the reproducibility contract."""

# v1 -> v2: the output-field section (confidence's 1-10 conviction scale,
# key_factors' five-item limit, probability_over's 0-1 range) moved into
# the instruction text after Anthropic's structured-outputs dialect forced
# those constraints out of the request JSON Schema. The response schema
# itself is unchanged, so FORECAST_SCHEMA_VERSION stays at v1.
BENCHMARK_PROMPT_VERSION = "benchmark-v2"
FORECAST_SCHEMA_VERSION = "forecast-v1"
