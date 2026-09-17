"""Frozen version identifiers. Bump only when the corresponding template
or schema actually changes -- these are persisted on every AgentSession
and are part of the reproducibility contract."""

# v1 -> v2: the output-field section (confidence's 1-10 conviction scale,
# key_factors' five-item limit, probability_over's 0-1 range) moved into
# the instruction text after Anthropic's structured-outputs dialect forced
# those constraints out of the request JSON Schema. The response schema
# itself is unchanged, so FORECAST_SCHEMA_VERSION stays at v1.
# v2 -> v3: market_context gained books_observed / stale_books_excluded /
# canonical_quote_stale, and the instruction text below explains what they
# mean. This is a PROMPT version bump because it changes what every
# competitor is shown; the response schema is untouched, so
# FORECAST_SCHEMA_VERSION stays at v1 for the same reason it did at v2.
#
# It is bumped rather than edited in place because prompt_version is
# persisted on every AgentSession and is the reproducibility contract: a
# season already running under benchmark-v2 must keep rendering
# benchmark-v2, not silently acquire new fields mid-season.
BENCHMARK_PROMPT_VERSION = "benchmark-v3"
FORECAST_SCHEMA_VERSION = "forecast-v1"
