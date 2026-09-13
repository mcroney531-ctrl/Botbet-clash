"""Real OpenAI adapter. Uses OpenAI's Structured Outputs (JSON Schema
response_format) on Chat Completions so the provider itself constrains
its reply to the shape validation.py expects -- content is still fully
re-validated there regardless; this adapter is only responsible for
turning an OpenAI response into a provider-neutral ProviderCallResult.

No OpenAI SDK type may leak past this module -- only `openai`-flavored
code lives here.
"""

from __future__ import annotations

import json

import openai

from app.ai.prompts.benchmark_forecasting import render_benchmark_prompt
from app.ai.providers.base import ProviderCallResult, error_result
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, benchmark_response_json_schema


class OpenAIAdapter:
    provider_name = "openai"

    def __init__(self, model_identifier: str, *, client: "openai.OpenAI | None" = None) -> None:
        # `openai.OpenAI()` reads OPENAI_API_KEY from the environment when
        # no api_key is passed -- credentials never touch this adapter's
        # own code.
        self.model_identifier = model_identifier
        self._client = client if client is not None else openai.OpenAI()

    def forecast_benchmark(self, request: BenchmarkForecastRequest) -> ProviderCallResult:
        rendered = render_benchmark_prompt(request)

        try:
            completion = self._client.chat.completions.create(
                model=self.model_identifier,
                messages=[
                    {"role": "system", "content": rendered["system"]},
                    {"role": "user", "content": json.dumps(rendered["user"])},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "benchmark_forecast_response",
                        "schema": benchmark_response_json_schema(),
                        "strict": True,
                    },
                },
            )
        except openai.AuthenticationError as exc:
            return self._error(category="AUTHENTICATION_ERROR", message=str(exc))
        except openai.RateLimitError as exc:
            return self._error(category="RATE_LIMITED", message=str(exc))
        except openai.APITimeoutError as exc:
            return self._error(category="TIMEOUT", message=str(exc))
        except openai.APIConnectionError as exc:
            return self._error(category="PROVIDER_UNAVAILABLE", message=str(exc))
        except openai.APIStatusError as exc:
            category = "PROVIDER_UNAVAILABLE" if exc.status_code >= 500 else "UNKNOWN_PROVIDER_ERROR"
            return self._error(category=category, message=str(exc))
        except openai.OpenAIError as exc:
            return self._error(category="UNKNOWN_PROVIDER_ERROR", message=str(exc))

        usage = completion.usage.model_dump() if completion.usage is not None else None
        choice = completion.choices[0]

        if choice.finish_reason == "content_filter":
            return self._error(
                category="CONTENT_REFUSAL",
                message="OpenAI finish_reason=content_filter",
                provider_request_id=completion.id,
                usage_metadata=usage,
            )

        raw_text = choice.message.content
        if not raw_text:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"empty response content (finish_reason={choice.finish_reason})",
                provider_request_id=completion.id,
                usage_metadata=usage,
            )

        try:
            parsed_payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"response was not valid JSON: {exc}",
                raw_response=raw_text,
                provider_request_id=completion.id,
                usage_metadata=usage,
            )

        return ProviderCallResult(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            raw_response=parsed_payload,
            parsed_payload=parsed_payload,
            provider_request_id=completion.id,
            usage_metadata=usage,
            error=None,
        )

    def _error(
        self,
        *,
        category,
        message: str,
        raw_response=None,
        provider_request_id: str | None = None,
        usage_metadata: dict | None = None,
    ) -> ProviderCallResult:
        return error_result(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            category=category,
            message=message,
            raw_response=raw_response,
            provider_request_id=provider_request_id,
            usage_metadata=usage_metadata,
        )
