"""Phase 3 acceptance test (real provider path, steps 15-24): the same
AIOrchestrator used against MockAdapter, now driving real OpenAI /
Anthropic / Gemini calls. Every test here is `live_provider`-marked and
skipped unless its provider's API key is present in the environment --
routine CI must never depend on a paid provider call. This file makes no
claim about which model "did better"; Phase 3 is a connectivity and
auditability test, not a research result.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ai.orchestrator import AIOrchestrator
from app.ai.registry import ProviderRegistry, live_registry
from app.ai.session_service import build_audit_receipt
from app.core.money import Money
from app.db.models.forecast_lab import AgentSession, EvidenceSnapshot, ForecastObservation
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.models import SeasonRules
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.services.season_commissioner import SeasonCommissioner

pytestmark = pytest.mark.live_provider

# Small/cheap model identifiers, deliberately not the season's eventual
# production roster (RULES.md's "known Week 0 questions... roster/version"
# is explicitly not decided here) -- update if a provider retires one.
LIVE_MODEL_IDENTIFIERS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "google": "gemini-2.0-flash",
}

HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))
HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
HAS_GOOGLE_KEY = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def make_rules() -> SeasonRules:
    return SeasonRules(
        rules_version="2026-live-provider-smoke",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


def _build_one_market_evidence(*, provider_label: str) -> tuple[SeasonCommissioner, uuid.UUID, uuid.UUID, uuid.UUID]:
    """One season, one competitor, one clean eligible market, its OPENING
    checkpoint captured. Returns (commissioner, season_competitor_id,
    checkpoint_run_id, evidence_snapshot_id)."""

    commissioner = SeasonCommissioner.create_season(
        name=f"Live Provider Smoke ({provider_label})", year=2026, rules=make_rules()
    )
    with session_scope() as session:
        active_rules_row = SeasonRepository(session).get_active_rules(commissioner.season_id)
        windows_config = dict(active_rules_row.checkpoint_windows)
        canonical_book = active_rules_row.canonical_sportsbook

    sc_id = commissioner.register_competitor(
        competitor_id=provider_label,
        provider=provider_label,
        display_name=provider_label.upper(),
        model_identifier=LIVE_MODEL_IDENTIFIERS[provider_label],
        model_version="live-smoke",
    )
    season_competitor_id = uuid.UUID(sc_id)

    commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=100)

    with session_scope() as session:
        market_repo = MarketRepository(session)
        game = market_repo.create_game(
            external_ref=f"live-smoke-{provider_label}-{uuid.uuid4()}", season_id=commissioner.season_id,
            week_number=1, home_team="KC", away_team="BUF", kickoff_at=kickoff,
        )
        player = market_repo.create_player(
            external_ref=f"live-smoke-player-{uuid.uuid4()}", name="Player X", team="KC", position="WR"
        )
        market = market_repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        market_repo.add_quote(
            market_id=market.id, sportsbook=canonical_book, line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_id, market_id = game.id, market.id

    with session_scope() as session:
        game_row = MarketRepository(session).get_game(game_id)
        run = capture_checkpoint(
            session, game=game_row, checkpoint_type="OPENING", windows_config=windows_config,
            now=now, canonical_sportsbook=canonical_book,
        )
        assert run.status == "CAPTURED"
        run_id = run.id
        evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(
                EvidenceSnapshot.market_id == market_id, EvidenceSnapshot.checkpoint_type == "OPENING"
            )
        ).scalar_one()

    return commissioner, season_competitor_id, run_id, evidence_id


def _assert_valid_single_forecast(agent_session_id: uuid.UUID, provider: str) -> None:
    with session_scope() as session:
        agent_session = session.get(AgentSession, agent_session_id)
        assert agent_session.status == "VALID"
        assert agent_session.provider == provider
        assert agent_session.raw_response is not None
        assert agent_session.validated_response is not None

        obs = session.query(ForecastObservation).filter(
            ForecastObservation.agent_session_id == agent_session_id
        ).one()
        assert Decimal("0") <= obs.model_probability_over <= Decimal("1")

        receipt = build_audit_receipt(session, agent_session_id)
        assert receipt["validated"] is True
        assert receipt["raw_response_preserved"] is True
        assert len(receipt["forecast_observation_ids"]) == 1


@pytest.mark.skipif(not HAS_OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_openai_adapter_produces_one_valid_forecast():
    commissioner, sc_id, run_id, evidence_id = _build_one_market_evidence(provider_label="openai")
    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=live_registry())
    agent_session_id = orchestrator.run_benchmark_forecast(
        season_competitor_id=sc_id, checkpoint_run_id=run_id, evidence_snapshot_ids=[evidence_id]
    )
    _assert_valid_single_forecast(agent_session_id, "openai")


@pytest.mark.skipif(not HAS_ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not set")
def test_anthropic_adapter_produces_one_valid_forecast():
    commissioner, sc_id, run_id, evidence_id = _build_one_market_evidence(provider_label="anthropic")
    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=live_registry())
    agent_session_id = orchestrator.run_benchmark_forecast(
        season_competitor_id=sc_id, checkpoint_run_id=run_id, evidence_snapshot_ids=[evidence_id]
    )
    _assert_valid_single_forecast(agent_session_id, "anthropic")


@pytest.mark.skipif(not HAS_GOOGLE_KEY, reason="GEMINI_API_KEY/GOOGLE_API_KEY not set")
def test_gemini_adapter_produces_one_valid_forecast():
    commissioner, sc_id, run_id, evidence_id = _build_one_market_evidence(provider_label="google")
    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=live_registry())
    agent_session_id = orchestrator.run_benchmark_forecast(
        season_competitor_id=sc_id, checkpoint_run_id=run_id, evidence_snapshot_ids=[evidence_id]
    )
    _assert_valid_single_forecast(agent_session_id, "google")


@pytest.mark.skipif(
    not (HAS_OPENAI_KEY and HAS_ANTHROPIC_KEY and HAS_GOOGLE_KEY),
    reason="requires OPENAI_API_KEY, ANTHROPIC_API_KEY, and GEMINI_API_KEY/GOOGLE_API_KEY all set",
)
def test_three_real_models_forecast_the_identical_frozen_evidence_snapshot():
    """The moment BotBet Clash has three real, independent, auditable
    competitors: GPT, Claude, and Gemini each see the exact same frozen
    EvidenceSnapshot and return their own independent forecast, with no
    leakage between them."""

    commissioner = SeasonCommissioner.create_season(
        name="Live Provider Smoke (three models)", year=2026, rules=make_rules()
    )
    with session_scope() as session:
        active_rules_row = SeasonRepository(session).get_active_rules(commissioner.season_id)
        windows_config = dict(active_rules_row.checkpoint_windows)
        canonical_book = active_rules_row.canonical_sportsbook

    season_competitor_ids = {}
    for provider_label in ("openai", "anthropic", "google"):
        sc_id = commissioner.register_competitor(
            competitor_id=provider_label, provider=provider_label, display_name=provider_label.upper(),
            model_identifier=LIVE_MODEL_IDENTIFIERS[provider_label], model_version="live-smoke",
        )
        season_competitor_ids[provider_label] = uuid.UUID(sc_id)

    commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=100)

    with session_scope() as session:
        market_repo = MarketRepository(session)
        game = market_repo.create_game(
            external_ref=f"live-smoke-three-{uuid.uuid4()}", season_id=commissioner.season_id,
            week_number=1, home_team="KC", away_team="BUF", kickoff_at=kickoff,
        )
        player = market_repo.create_player(
            external_ref=f"live-smoke-three-player-{uuid.uuid4()}", name="Player X", team="KC", position="WR"
        )
        market = market_repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        market_repo.add_quote(
            market_id=market.id, sportsbook=canonical_book, line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_id, market_id = game.id, market.id

    with session_scope() as session:
        game_row = MarketRepository(session).get_game(game_id)
        run = capture_checkpoint(
            session, game=game_row, checkpoint_type="OPENING", windows_config=windows_config,
            now=now, canonical_sportsbook=canonical_book,
        )
        run_id = run.id
        evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(
                EvidenceSnapshot.market_id == market_id, EvidenceSnapshot.checkpoint_type == "OPENING"
            )
        ).scalar_one()

    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=live_registry())
    session_ids = orchestrator.run_benchmark_round(
        season_competitor_ids=list(season_competitor_ids.values()),
        checkpoint_run_id=run_id,
        evidence_snapshot_ids=[evidence_id],
    )
    assert len(session_ids) == 3
    assert len(set(session_ids.values())) == 3

    rendered_by_provider = {}
    with session_scope() as fresh_session:
        for provider_label, sc_id in season_competitor_ids.items():
            agent_session = fresh_session.get(AgentSession, session_ids[sc_id])
            assert agent_session.status == "VALID"
            assert agent_session.provider == provider_label
            assert agent_session.model_identifier == LIVE_MODEL_IDENTIFIERS[provider_label]
            rendered_by_provider[provider_label] = agent_session.rendered_request

            obs = fresh_session.query(ForecastObservation).filter(
                ForecastObservation.agent_session_id == agent_session.id
            ).one()
            assert obs.evidence_snapshot_id == evidence_id
            assert Decimal("0") <= obs.model_probability_over <= Decimal("1")

            receipt = build_audit_receipt(fresh_session, agent_session.id)
            assert receipt["competitor"] == provider_label.upper()
            assert receipt["input_evidence_snapshot_ids"] == [str(evidence_id)]

    # Every competitor's rendered request carries the identical canonical
    # market/evidence content -- none of them ever saw another's answer.
    assert rendered_by_provider["openai"]["user"] == rendered_by_provider["anthropic"]["user"]
    assert rendered_by_provider["anthropic"]["user"] == rendered_by_provider["google"]["user"]
