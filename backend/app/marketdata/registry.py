"""Provider name -> adapter. The only place a provider is constructed.

Mirrors app/ai/registry.py. Callers resolve by the frozen
SeasonRules.market_data_provider string, so nothing outside this module
needs to know which concrete class is in play.
"""

from __future__ import annotations

from app.marketdata.base import MarketDataProvider
from app.marketdata.providers.mock import MOCK_PROVIDER_NAME, MockMarketDataProvider
from app.marketdata.providers.the_odds_api import PROVIDER_NAME as THE_ODDS_API_NAME
from app.marketdata.providers.the_odds_api import TheOddsApiProvider


def build_provider(name: str, **kwargs) -> MarketDataProvider:
    if name == THE_ODDS_API_NAME:
        return TheOddsApiProvider(**kwargs)
    if name == MOCK_PROVIDER_NAME:
        return MockMarketDataProvider(**kwargs)
    raise KeyError(
        f"unknown market-data provider {name!r}; "
        f"expected one of {{{THE_ODDS_API_NAME!r}, {MOCK_PROVIDER_NAME!r}}}"
    )


def live_registry(**kwargs) -> MarketDataProvider:
    """The real provider. Separated from build_provider so tests can never
    reach it by passing a string through."""

    return TheOddsApiProvider(**kwargs)
