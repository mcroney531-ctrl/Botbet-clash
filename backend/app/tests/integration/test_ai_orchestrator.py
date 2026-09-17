"""Phase 3A acceptance test (mock path): the AIOrchestrator drives
MockAdapter end to end -- orchestrator -> adapter -> validation ->
AgentSession -> ForecastObservation -- against real PostgreSQL, proving
the exact reproducibility/auditability/failure-handling behavior the
Phase 3 brief asks for, without any real provider call.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.ai.orchestrator import AIOrchestrator
from app.ai.providers.mock import MockOutcome
from app.ai.registry import mock_registry
from app.ai.session_service import AgentSessionRepository, build_audit_receipt
from app.core.money import Money
from app.db.models.forecast_lab import AgentSession, AgentSessionEvidenceSnapshot, EvidenceSnapshot, ForecastObservation
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.models import SeasonRules
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.services.season_commissioner import SeasonCommissioner


def make_rules() -> SeasonRules:
    return SeasonRules(
        rules_version="2026-ai-orchestrator",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


class PoisonRegistry:
    """A registry that fails loudly if it's ever asked to resolve an
    adapter -- used to prove idempotency short-circuits before any
    provider is ever contacted again."""

    def for_competitor(self, session, season_competitor_id):
        raise AssertionError("registry.for_competitor was called on an already-VALID orchestration_key")


def _capture_market(session, *, game, checkpoint_type, windows_config, now, canonical_book):
    run = capture_checkpoint(
        session, game=game, checkpoint_type=checkpoint_type, windows_config=windows_config,
        now=now, canonical_sportsbook=canonical_book,
    )
    assert run.status == "CAPTURED"
    return run


def test_ai_orchestrator_mock_path_end_to_end():
    # ---- Season + rules + three season-competitors ----------------------
    commissioner = SeasonCommissioner.create_season(name="AI Orchestrator Mock Path", year=2026, rules=make_rules())

    with session_scope() as session:
        active_rules_row = SeasonRepository(session).get_active_rules(commissioner.season_id)
        windows_config = dict(active_rules_row.checkpoint_windows)
        canonical_book = active_rules_row.canonical_sportsbook

    competitor_ids: dict[str, uuid.UUID] = {}
    for competitor_id, provider in (("openai", "openai"), ("anthropic", "anthropic"), ("google", "google")):
        sc_id = commissioner.register_competitor(
            competitor_id=competitor_id, provider=provider, display_name=competitor_id.upper(),
            model_identifier=f"{competitor_id}-mock-model", model_version="v1",
        )
        competitor_ids[competitor_id] = uuid.UUID(sc_id)
    gpt_id, claude_id, gemini_id = competitor_ids["openai"], competitor_ids["anthropic"], competitor_ids["google"]

    week_id = commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff_a = now + timedelta(hours=100)
    kickoff_b = now + timedelta(hours=100)

    # ---- Game A: the happy path + idempotency check ----------------------
    with session_scope() as session:
        market_repo = MarketRepository(session)
        game_a = market_repo.create_game(
            external_ref="ai-orch-game-a", season_id=commissioner.season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=kickoff_a,
        )
        player_a = market_repo.create_player(external_ref="ai-orch-player-a", name="Player A", team="KC", position="WR")
        market_repo.create_game_player(game_id=game_a.id, player_id=player_a.id, team=game_a.home_team_canonical)
        market_a = market_repo.create_prop_market(game_id=game_a.id, player_id=player_a.id, stat_type="receiving_yards")
        market_repo.add_quote(
            market_id=market_a.id, sportsbook=canonical_book, line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_a_id, market_a_id = game_a.id, market_a.id

    with session_scope() as session:
        game_a_row = MarketRepository(session).get_game(game_a_id)
        run_a = _capture_market(session, game=game_a_row, checkpoint_type="OPENING", windows_config=windows_config, now=now, canonical_book=canonical_book)
        run_a_id = run_a.id
        evidence_a_id = session.execute(
            select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_a_id, EvidenceSnapshot.checkpoint_type == "OPENING")
        ).scalar_one()

    happy_registry = mock_registry(
        fixtures_by_provider={
            "openai": {str(market_a_id): 0.61},
            "anthropic": {str(market_a_id): 0.55},
            "google": {str(market_a_id): 0.58},
        }
    )
    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=happy_registry)

    session_ids = orchestrator.run_benchmark_round(
        season_competitor_ids=[gpt_id, claude_id, gemini_id],
        checkpoint_run_id=run_a_id,
        evidence_snapshot_ids=[evidence_a_id],
    )
    assert len(session_ids) == 3
    assert len(set(session_ids.values())) == 3  # three distinct AgentSessions

    expected_probs = {gpt_id: Decimal("0.61"), claude_id: Decimal("0.55"), gemini_id: Decimal("0.58")}
    expected_providers = {gpt_id: "openai", claude_id: "anthropic", gemini_id: "google"}

    with session_scope() as session:
        for sc_id, agent_session_id in session_ids.items():
            agent_session = session.get(AgentSession, agent_session_id)
            assert agent_session.status == "VALID"
            assert agent_session.is_valid is True
            assert agent_session.provider == expected_providers[sc_id]
            assert agent_session.model_identifier.startswith(expected_providers[sc_id])
            assert agent_session.call_type == "BENCHMARK_FORECASTING"
            assert agent_session.raw_response is not None
            assert agent_session.validated_response is not None
            assert agent_session.transport_retry_count == 0
            assert agent_session.correction_retry_count == 0
            assert agent_session.rendered_request["system"]  # neutral prompt was stored verbatim

            # Every session points at exactly the evidence it was given -
            # the join table, not a singular column, is authoritative.
            links = session.query(AgentSessionEvidenceSnapshot).filter(
                AgentSessionEvidenceSnapshot.agent_session_id == agent_session_id
            ).all()
            assert [link.evidence_snapshot_id for link in links] == [evidence_a_id]

            obs = session.query(ForecastObservation).filter(
                ForecastObservation.agent_session_id == agent_session_id
            ).one()
            assert obs.season_competitor_id == sc_id
            assert obs.market_id == market_a_id
            assert obs.model_probability_over == expected_probs[sc_id]
            assert obs.evidence_snapshot_id == evidence_a_id
            assert obs.research_eligible is True

    # No competitor's response leaked into another's persisted request.
    with session_scope() as session:
        rendered_by_competitor = {
            sc_id: session.get(AgentSession, sid).rendered_request for sc_id, sid in session_ids.items()
        }
    for sc_id, rendered in rendered_by_competitor.items():
        payload_str = str(rendered)
        for other_id, other_prob in expected_probs.items():
            if other_id == sc_id:
                continue
            # The only numbers in a rendered request are canonical market
            # data, identical for all three - a competing model's own
            # *forecast* value never appears in another's input.
            assert str(other_prob) not in payload_str or other_prob == expected_probs[sc_id]

    # ---- Idempotency: rerunning gpt against the same checkpoint must not
    # call the provider again, and must not create a second AgentSession --
    poison_orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=PoisonRegistry())
    replay_id = poison_orchestrator.run_benchmark_forecast(
        season_competitor_id=gpt_id, checkpoint_run_id=run_a_id, evidence_snapshot_ids=[evidence_a_id],
    )
    assert replay_id == session_ids[gpt_id]

    with session_scope() as session:
        count = (
            session.query(AgentSession)
            .filter(AgentSession.season_competitor_id == gpt_id, AgentSession.call_type == "BENCHMARK_FORECASTING")
            .count()
        )
    assert count == 1

    # ---- Reload from a fresh session and reconstruct via audit receipts -
    with session_scope() as session:
        receipts = {sc_id: build_audit_receipt(session, sid) for sc_id, sid in session_ids.items()}
    for sc_id, receipt in receipts.items():
        assert receipt["provider"] == expected_providers[sc_id]
        assert receipt["validated"] is True
        assert receipt["raw_response_preserved"] is True
        assert receipt["input_evidence_snapshot_ids"] == [str(evidence_a_id)]
        assert len(receipt["forecast_observation_ids"]) == 1

    # ---- Game B: correction retry (success), correction retry (exhausted,
    # rejected), and immediate transport failure -- one competitor each --
    with session_scope() as session:
        market_repo = MarketRepository(session)
        game_b = market_repo.create_game(
            external_ref="ai-orch-game-b", season_id=commissioner.season_id, week_number=1,
            home_team="SF", away_team="DAL", kickoff_at=kickoff_b,
        )
        player_b = market_repo.create_player(external_ref="ai-orch-player-b", name="Player B", team="SF", position="QB")
        market_repo.create_game_player(game_id=game_b.id, player_id=player_b.id, team=game_b.home_team_canonical)
        market_b = market_repo.create_prop_market(game_id=game_b.id, player_id=player_b.id, stat_type="passing_yards")
        market_repo.add_quote(
            market_id=market_b.id, sportsbook=canonical_book, line=Decimal("225.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_b_id, market_b_id = game_b.id, market_b.id

    with session_scope() as session:
        game_b_row = MarketRepository(session).get_game(game_b_id)
        run_b = _capture_market(session, game=game_b_row, checkpoint_type="OPENING", windows_config=windows_config, now=now, canonical_book=canonical_book)
        run_b_id = run_b.id
        evidence_b_id = session.execute(
            select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_b_id, EvidenceSnapshot.checkpoint_type == "OPENING")
        ).scalar_one()

    failure_registry = mock_registry(
        fixtures_by_provider={
            "openai": {str(market_b_id): 0.64},
            "anthropic": {str(market_b_id): 0.50},
            "google": {str(market_b_id): 0.50},
        },
        call_plans_by_provider={
            "openai": [MockOutcome.missing_market(), MockOutcome.valid()],  # succeeds after 1 correction
            "anthropic": [MockOutcome.probability_out_of_range(), MockOutcome.invalid_confidence()],  # rejected
            "google": [MockOutcome.timeout()],  # no usable answer at all
        },
    )
    failure_orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=failure_registry)
    b_session_ids = failure_orchestrator.run_benchmark_round(
        season_competitor_ids=[gpt_id, claude_id, gemini_id],
        checkpoint_run_id=run_b_id,
        evidence_snapshot_ids=[evidence_b_id],
    )

    with session_scope() as session:
        gpt_session = session.get(AgentSession, b_session_ids[gpt_id])
        assert gpt_session.status == "VALID"
        assert gpt_session.correction_retry_count == 1
        assert gpt_session.transport_retry_count == 0
        gpt_obs = session.query(ForecastObservation).filter(ForecastObservation.agent_session_id == gpt_session.id).one()
        assert gpt_obs.model_probability_over == Decimal("0.64")

        claude_session = session.get(AgentSession, b_session_ids[claude_id])
        assert claude_session.status == "INVALID"
        assert claude_session.is_valid is False
        assert claude_session.correction_retry_count == 1
        assert claude_session.error_category == "SCHEMA_VALIDATION_FAILED"
        assert claude_session.raw_response is not None  # preserved even though rejected
        assert claude_session.validated_response is None
        claude_obs_count = session.query(ForecastObservation).filter(
            ForecastObservation.agent_session_id == claude_session.id
        ).count()
        assert claude_obs_count == 0  # a rejected session must never produce a fake observation

        gemini_session = session.get(AgentSession, b_session_ids[gemini_id])
        assert gemini_session.status == "FAILED"
        assert gemini_session.is_valid is False
        assert gemini_session.error_category == "TIMEOUT"
        assert gemini_session.raw_response is None
        gemini_obs_count = session.query(ForecastObservation).filter(
            ForecastObservation.agent_session_id == gemini_session.id
        ).count()
        assert gemini_obs_count == 0

    # A failed/rejected competitor never silently poisons the others: the
    # successful gpt call at game B is untouched by claude's/gemini's fate.
    with session_scope() as session:
        repo = AgentSessionRepository(session)
        assert repo.get(b_session_ids[gpt_id]).status == "VALID"


class _ExplodingAdapter:
    """A real adapter that misbehaves: raises a raw, unnormalized
    exception instead of catching it and returning a ProviderCallResult.
    Proves the orchestrator's backstop (found via a live Gemini run that
    let an uncaught httpx.ConnectError escape) still reaches a terminal
    FAILED status rather than leaving the session stuck at CALLING."""

    provider_name = "openai"

    def __init__(self, model_identifier: str) -> None:
        self.model_identifier = model_identifier

    def forecast_benchmark(self, request):
        raise RuntimeError("simulated: adapter forgot to catch its own SDK's exception")


def test_an_adapter_that_raises_still_reaches_a_terminal_failed_status():
    commissioner = SeasonCommissioner.create_season(name="Orchestrator Backstop", year=2026, rules=make_rules())

    with session_scope() as session:
        active_rules_row = SeasonRepository(session).get_active_rules(commissioner.season_id)
        windows_config = dict(active_rules_row.checkpoint_windows)
        canonical_book = active_rules_row.canonical_sportsbook

    sc_id = uuid.UUID(
        commissioner.register_competitor(
            competitor_id="openai", provider="openai", display_name="OPENAI",
            model_identifier="openai-mock-model", model_version="v1",
        )
    )
    commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=100)

    with session_scope() as session:
        market_repo = MarketRepository(session)
        game = market_repo.create_game(
            external_ref="backstop-game", season_id=commissioner.season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=kickoff,
        )
        player = market_repo.create_player(external_ref="backstop-player", name="Player X", team="KC", position="WR")
        market_repo.create_game_player(game_id=game.id, player_id=player.id, team=game.home_team_canonical)
        market = market_repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        market_repo.add_quote(
            market_id=market.id, sportsbook=canonical_book, line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_id, market_id = game.id, market.id

    with session_scope() as session:
        game_row = MarketRepository(session).get_game(game_id)
        run = _capture_market(session, game=game_row, checkpoint_type="OPENING", windows_config=windows_config, now=now, canonical_book=canonical_book)
        run_id = run.id
        evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_id, EvidenceSnapshot.checkpoint_type == "OPENING")
        ).scalar_one()

    exploding_registry = type(
        "ExplodingRegistry", (), {"for_competitor": staticmethod(lambda session, sc_id: _ExplodingAdapter("openai-mock-model"))}
    )()
    orchestrator = AIOrchestrator(season_id=commissioner.season_id, registry=exploding_registry)

    agent_session_id = orchestrator.run_benchmark_forecast(
        season_competitor_id=sc_id, checkpoint_run_id=run_id, evidence_snapshot_ids=[evidence_id],
    )

    with session_scope() as session:
        agent_session = session.get(AgentSession, agent_session_id)
        assert agent_session.status == "FAILED"  # not stuck at CALLING
        assert agent_session.is_valid is False
        assert agent_session.error_category == "UNKNOWN_PROVIDER_ERROR"
        assert "RuntimeError" in agent_session.error_message
        obs_count = session.query(ForecastObservation).filter(
            ForecastObservation.agent_session_id == agent_session_id
        ).count()
        assert obs_count == 0
