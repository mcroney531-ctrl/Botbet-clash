"""Real Gemini adapter (google-genai SDK). Uses `response_json_schema` +
`response_mime_type="application/json"` so the model's raw text is
schema-conformant JSON; validation.py still fully re-validates the
content regardless. No google.genai type may leak past this module.
"""

from __future__ import annotations

import json

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.ai.prompts.benchmark_forecasting import render_benchmark_prompt
from app.ai.providers.base import ProviderCallResult, error_result
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, benchmark_response_json_schema


class GeminiAdapter:
    provider_name = "google"

    def __init__(self, model_identifier: str, *, client: "genai.Client | None" = None) -> None:
        # `genai.Client()` reads GEMINI_API_KEY or GOOGLE_API_KEY from the
        # environment when no api_key is passed.
        self.model_identifier = model_identifier
        self._client = client if client is not None else genai.Client()

    def forecast_benchmark(self, request: BenchmarkForecastRequest) -> ProviderCallResult:
        rendered = render_benchmark_prompt(request)

        try:
            response = self._client.models.generate_content(
                model=self.model_identifier,
                contents=json.dumps(rendered["user"]),
                config=genai_types.GenerateContentConfig(
                    system_instruction=rendered["system"],
                    response_mime_type="application/json",
                    response_json_schema=benchmark_response_json_schema(),
                ),
            )
        except genai_errors.ClientError as exc:
            category = self._client_error_category(exc)
            return self._error(category=category, message=str(exc))
        except genai_errors.ServerError as exc:
            return self._error(category="PROVIDER_UNAVAILABLE", message=str(exc))
        except httpx.TimeoutException as exc:
            return self._error(category="TIMEOUT", message=str(exc))
        except genai_errors.APIError as exc:
            return self._error(category="UNKNOWN_PROVIDER_ERROR", message=str(exc))

        usage = response.usage_metadata.model_dump() if response.usage_metadata is not None else None
        candidates = response.candidates or []
        finish_reason = candidates[0].finish_reason if candidates else None
        finish_reason_value = finish_reason.value if finish_reason is not None else None

        if finish_reason_value in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            return self._error(
                category="CONTENT_REFUSAL",
                message=f"Gemini finish_reason={finish_reason_value}",
                usage_metadata=usage,
            )

        raw_text = response.text
        if not raw_text:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"empty response text (finish_reason={finish_reason_value})",
                usage_metadata=usage,
            )

        try:
            parsed_payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return self._error(
                category="INVALID_PROVIDER_RESPONSE",
                message=f"response was not valid JSON: {exc}",
                raw_response=raw_text,
                usage_metadata=usage,
            )

        return ProviderCallResult(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            raw_response=parsed_payload,
            parsed_payload=parsed_payload,
            provider_request_id=response.response_id,
            usage_metadata=usage,
            error=None,
        )

    @staticmethod
    def _client_error_category(exc: "genai_errors.ClientError") -> str:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return "AUTHENTICATION_ERROR"
        if code == 429:
            return "RATE_LIMITED"
        return "UNKNOWN_PROVIDER_ERROR"

    def _error(
        self,
        *,
        category,
        message: str,
        raw_response=None,
        usage_metadata: dict | None = None,
    ) -> ProviderCallResult:
        return error_result(
            provider=self.provider_name,
            model_identifier=self.model_identifier,
            category=category,
            message=message,
            raw_response=raw_response,
            usage_metadata=usage_metadata,
        )
