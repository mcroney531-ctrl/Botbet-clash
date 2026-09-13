from decimal import Decimal

import pytest

from app.core.clock import FixedClock
from app.core.money import Money
from app.domain.events import CompetitionEventBus
from app.domain.ledger import BankrollLedger
from app.domain.models import Competitor, Season, SeasonRules, Week
from app.domain.season_service import SeasonService


@pytest.fixture
def season_rules() -> SeasonRules:
    return SeasonRules(
        rules_version="2026-w0.1",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def service(season_rules: SeasonRules, clock: FixedClock) -> SeasonService:
    season = Season(name="2026 NFL Season (Week 0 harness)", year=2026, rules=season_rules)
    ledger = BankrollLedger(clock)
    bus = CompetitionEventBus()
    svc = SeasonService(season=season, clock=clock, ledger=ledger, event_bus=bus)
    for competitor_id, provider in (("openai", "OpenAI"), ("anthropic", "Anthropic"), ("google", "Google")):
        svc.register_competitor(
            Competitor(id=competitor_id, provider=provider, model_identifier="placeholder", model_version="v0")
        )
    return svc


@pytest.fixture
def week1(service: SeasonService) -> Week:
    week = Week(season_id=service.season.id, week_number=1, is_real_money=True)
    return service.open_week(week)
