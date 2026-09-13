"""Real Anthropic adapter. Uses Claude's structured-outputs `output_config`
(a JSON Schema format constraint on `messages.create`) so the response
text is schema-conformant JSON; validation.py still fully re-validates
the content regardless. No Anthropic SDK type may leak past this module.
"""

from __future__ import annotations

import json

import anthropic

from app.ai.prompts.benchmark_forecasting import render_benchmark_prompt
from app.ai.providers.base import ProviderCallResult, error_result
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, benchmark_response_json_schema

MAX_TOKENS = 4096


class AnthropicAdapter:
    provider_name = "anthropic"

    def __init__(self, model_identifier: str, *, client: "anthropic.Anthropic | None" = None) -> None:
        # `anthropic.Anthropic()` reads ANTHROPIC_API_KEY from the
        # environment when no api_key is passed.
        self.model_identifier = model_identifier
        self._client = client if client is not None else anthropic.Anthropic()

    def forecast_benchmark(self, request: BenchmarkForecastRequest) -> ProviderCallResult:
        rendered = render_benchmark_prompt(request)

        try:
            message = self._client.messages.create(
                model=self.model_identifier,
                max_tokens=MAX_TOKENS,
                system=rendered["system"],
                messages=[{"role": "user", "content": json.dumps(rendered["user"])}],
                output_config={
                    "format": {"type": "json_schema", "schema": benchmark_response_json_schema()},
                },
            )
        except anthropic.AuthenticationError as exc:
            return self._error(category="AUTHENTICATION_ERROR", message=str(exc))
        except anthropic.RateLimitError as exc:
            return self._error(category="RATE_LIMITED", message=str(exc))
        except anthropic.APITimeoutError as exc:
            return self._error(category="TIMEOUT", message=str(exc))
        except anthropic.APIConnectionError as exc:
            return self._error(category="PROVIDER_UNAVAILABLE", message=str(exc))
        except anthropic.APIStatusError as exc:
            category = "PROVIDER_UNAVAILABLE" if exc.status_code >= 500 else "UNKNOWN_PROVIDER_ERROR"
            return self._error(category=category, message=str(exc))
        except anthropic.AnthropicError as exc:
            return self._error(category="UNKNOWN_PROVIDER_ERROR", message=str(exc))

        usage = message.usage.model_dump() if message.usage is not None else None

        if message.stop_reason == "refusal":
            return self._error(
                category="CONTENT_REFUSAL",
                message="Claude stop_reason=refusal",
                provider_request_id=message.id,
                usage_metadata=usage,
            )

        raw_text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        if not raw_text:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"no text content in response (stop_reason={message.stop_reason})",
                provider_request_id=message.id,
                usage_metadata=usage,
            )

        try:
            parsed_payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"response was not valid JSON: {exc}",
                raw_response=raw_text,
                provider_request_id=message.id,
                usage_metadata=usage,
            )

        return ProviderCallResult(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            raw_response=parsed_payload,
            parsed_payload=parsed_payload,
            provider_request_id=message.id,
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
