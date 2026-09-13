"""Provider registry: the only place that maps a persisted provider
string to an adapter. AIOrchestrator resolves adapters exclusively
through `for_competitor` -- provider is read from `Competitor.provider`
and model_identifier from `SeasonCompetitor.model_identifier`, never
inferred from a display name or hard-coded in adapter logic. Swapping
MockAdapter for a real adapter in Phase 3B only ever changes how a
registry is *constructed*, never the orchestrator.
"""

from __future__ import annotations

import uuid
from typing import Callable

from sqlalchemy.orm import Session

from app.ai.providers.base import CompetitorAdapter
from app.db.models.season import Competitor, SeasonCompetitor

# Given a model_identifier (sourced from persisted SeasonCompetitor
# config), returns an adapter instance configured for that model.
AdapterFactory = Callable[[str], CompetitorAdapter]


class ProviderRegistry:
    def __init__(self, factories: dict[str, AdapterFactory]) -> None:
        self._factories = factories

    def factory_for(self, provider: str) -> AdapterFactory:
        try:
            return self._factories[provider]
        except KeyError:
            raise LookupError(f"no adapter factory registered for provider {provider!r}") from None

    def for_competitor(self, session: Session, season_competitor_id: uuid.UUID) -> CompetitorAdapter:
        season_competitor = session.get(SeasonCompetitor, season_competitor_id)
        if season_competitor is None:
            raise LookupError(f"season_competitor {season_competitor_id} not found")
        competitor = session.get(Competitor, season_competitor.competitor_id)
        if competitor is None:
            raise LookupError(f"competitor {season_competitor.competitor_id} not found")
        factory = self.factory_for(competitor.provider)
        return factory(season_competitor.model_identifier)


def mock_registry(
    *, fixtures_by_provider: dict[str, dict[str, float]], call_plans_by_provider: dict[str, list] | None = None
) -> ProviderRegistry:
    """Test/dev convenience: routes every known provider string through a
    MockAdapter, so the exact same run_benchmark_round used against real
    adapters in Phase 3B can be exercised end-to-end today."""

    from app.ai.providers.mock import MockAdapter

    call_plans = call_plans_by_provider or {}

    def _factory_for(provider: str) -> AdapterFactory:
        fixtures = fixtures_by_provider[provider]
        plan = list(call_plans.get(provider, []))
        return lambda model_identifier: MockAdapter(
            model_identifier=model_identifier, fixtures=fixtures, call_plan=list(plan)
        )

    return ProviderRegistry({provider: _factory_for(provider) for provider in fixtures_by_provider})


def live_registry() -> ProviderRegistry:
    """The Phase 3B production mapping (brief's provider table): real
    adapters, one per provider string as persisted on `Competitor.provider`.
    Each adapter reads its own credentials from the environment lazily, at
    first use -- constructing this registry never requires API keys to be
    present.
    """

    from app.ai.providers.anthropic import AnthropicAdapter
    from app.ai.providers.gemini import GeminiAdapter
    from app.ai.providers.openai import OpenAIAdapter

    return ProviderRegistry(
        {
            "openai": lambda model_identifier: OpenAIAdapter(model_identifier),
            "anthropic": lambda model_identifier: AnthropicAdapter(model_identifier),
            "google": lambda model_identifier: GeminiAdapter(model_identifier),
        }
    )
