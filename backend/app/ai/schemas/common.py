"""Shared types for app/ai schemas. See PHASE 3 brief: no provider-specific
types may leak past the adapter boundary, so these are the provider-neutral
vocabulary every request/response schema is built from."""

from __future__ import annotations

from typing import Literal

from app.domain.enums import Uncertainty

__all__ = ["Uncertainty", "CallType"]

# Only BENCHMARK_FORECASTING exists in Phase 3. OPEN_MARKET_RESEARCH,
# MARKET_EVENT_REVIEW, STAKE_SIZING, FINAL_DECISION, POSTMORTEM,
# PUBLIC_COMMENTARY, and TRASH_TALK are explicitly out of scope.
CallType = Literal["BENCHMARK_FORECASTING"]
