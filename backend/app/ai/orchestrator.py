"""AIOrchestrator: the only code that turns persisted evidence into a
provider call and, from a validated response, a ForecastObservation. It
never mutates bankroll, settles wagers, or applies competition rules --
"the backend remains authoritative, the model supplies judgment." No
provider-specific branching lives here; everything provider-specific is
behind `registry.for_competitor(...)`.

Lifecycle (network calls must never happen inside an open DB
transaction): PENDING is created and committed first; the provider is
called with no transaction open; the outcome (VALID/INVALID/FAILED) plus
any ForecastObservations are then persisted in a second transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.ai.prompts.benchmark_forecasting import render_benchmark_prompt
from app.ai.prompts.versions import BENCHMARK_PROMPT_VERSION, FORECAST_SCHEMA_VERSION
from app.ai.providers.base import CompetitorAdapter, ProviderCallResult, ProviderError
from app.ai.registry import ProviderRegistry
from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, MarketContext, MarketInput
from app.ai.session_service import AgentSessionRepository, build_orchestration_key
from app.ai.validation import ValidationResult, validate_benchmark_response
from app.db.models.markets import Player
from app.db.models.season import Competitor, SeasonCompetitor
from app.db.repositories.ledger_repository import LedgerRepository
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import Uncertainty
from app.domain.errors import CrossSeasonReference
from app.forecast_lab.evidence_service import get_evidence_snapshot
from app.forecast_lab.forecast_service import create_forecast_observation

CALL_TYPE_BENCHMARK_FORECASTING = "BENCHMARK_FORECASTING"


class _MarketBatch:
    """Internal: the deterministic per-market inputs for one call, keyed
    both for schema-building and for wiring validated forecasts back to
    the right (market_id, evidence_snapshot_id) pair afterwards."""

    def __init__(self) -> None:
        self.inputs: list[MarketInput] = []
        # market_id (str, matches MarketInput.market_id) -> (market uuid, evidence_snapshot uuid)
        self.lookup: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}

    def add(self, market_input: MarketInput, market_id: uuid.UUID, evidence_snapshot_id: uuid.UUID) -> None:
        self.inputs.append(market_input)
        self.lookup[market_input.market_id] = (market_id, evidence_snapshot_id)

    def sorted_inputs(self) -> list[MarketInput]:
        # Deterministic ordering (never rely on unordered DB iteration).
        return sorted(self.inputs, key=lambda m: m.market_id)

    def evidence_snapshot_ids_by_market(self) -> dict[uuid.UUID, uuid.UUID]:
        return {market_id: evidence_id for market_id, evidence_id in self.lookup.values()}


class AIOrchestrator:
    """Scoped to one season, mirroring SeasonCommissioner's own
    cross-season-reference guarding."""

    def __init__(self, season_id: uuid.UUID, registry: ProviderRegistry, *, max_correction_retries: int = 1) -> None:
        self.season_id = season_id
        self.registry = registry
        self.max_correction_retries = max_correction_retries

    # -- public API ---------------------------------------------------

    def run_benchmark_forecast(
        self,
        *,
        season_competitor_id: uuid.UUID,
        checkpoint_run_id: uuid.UUID,
        evidence_snapshot_ids: list[uuid.UUID],
    ) -> uuid.UUID:
        """Run one competitor's BENCHMARK_FORECASTING call against the
        given frozen EvidenceSnapshots. Returns the AgentSession id.
        Idempotent: a rerun with the same (season_competitor_id,
        checkpoint_run_id) that already produced a VALID session returns
        that session's id without calling the provider again.
        """

        orchestration_key = build_orchestration_key(
            season_competitor_id=season_competitor_id,
            checkpoint_run_id=checkpoint_run_id,
            call_type=CALL_TYPE_BENCHMARK_FORECASTING,
            prompt_version=BENCHMARK_PROMPT_VERSION,
        )

        agent_session_id, already_valid, adapter, request, batch = self._prepare(
            season_competitor_id=season_competitor_id,
            evidence_snapshot_ids=evidence_snapshot_ids,
            orchestration_key=orchestration_key,
        )
        if already_valid:
            return agent_session_id

        with session_scope() as session:
            repo = AgentSessionRepository(session)
            repo.mark_calling(repo.get(agent_session_id))

        result, validation, transport_retry_count, correction_retry_count = self._call_with_corrections(
            adapter, request
        )

        with session_scope() as session:
            repo = AgentSessionRepository(session)
            agent_session = repo.get(agent_session_id)

            if result.error is not None:
                repo.mark_failed(
                    agent_session,
                    error_category=result.error.category,
                    error_message=result.error.message,
                    transport_retry_count=transport_retry_count,
                )
                return agent_session_id

            assert validation is not None
            if not validation.is_valid:
                repo.mark_invalid(
                    agent_session,
                    raw_response=result.raw_response,
                    error_category="SCHEMA_VALIDATION_FAILED",
                    error_message="; ".join(validation.errors),
                    transport_retry_count=transport_retry_count,
                    correction_retry_count=correction_retry_count,
                )
                return agent_session_id

            repo.mark_valid(
                agent_session,
                raw_response=result.raw_response,
                validated_response=validation.validated_response,
                provider_request_id=result.provider_request_id,
                usage_metadata=result.usage_metadata,
                transport_retry_count=transport_retry_count,
                correction_retry_count=correction_retry_count,
            )

            assert validation.forecasts is not None
            for forecast in validation.forecasts:
                market_id, evidence_snapshot_id = batch.lookup[forecast.market_id]
                create_forecast_observation(
                    session,
                    season_competitor_id=season_competitor_id,
                    market_id=market_id,
                    source_type="BENCHMARK",
                    checkpoint_type=request.checkpoint,
                    timestamp=agent_session.completed_at,
                    model_probability_over=forecast.probability_over,
                    evidence_snapshot_id=evidence_snapshot_id,
                    confidence=forecast.confidence,
                    uncertainty=Uncertainty(forecast.uncertainty.value),
                    agent_session_id=agent_session_id,
                )

            return agent_session_id

    def run_benchmark_round(
        self,
        *,
        season_competitor_ids: list[uuid.UUID],
        checkpoint_run_id: uuid.UUID,
        evidence_snapshot_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Run the identical frozen evidence_snapshot_ids against every
        given competitor, sequentially. Each call builds its own request
        from scratch (immutable snapshots + neutral prompt) -- no
        competitor's response is ever available when building another's
        request, so there is no leakage between them regardless of
        execution order.
        """

        return {
            season_competitor_id: self.run_benchmark_forecast(
                season_competitor_id=season_competitor_id,
                checkpoint_run_id=checkpoint_run_id,
                evidence_snapshot_ids=evidence_snapshot_ids,
            )
            for season_competitor_id in season_competitor_ids
        }

    # -- internals ------------------------------------------------------

    def _prepare(
        self,
        *,
        season_competitor_id: uuid.UUID,
        evidence_snapshot_ids: list[uuid.UUID],
        orchestration_key: str,
    ) -> tuple[uuid.UUID, bool, CompetitorAdapter | None, BenchmarkForecastRequest | None, "_ResolvedBatch | None"]:
        """Transaction 1: idempotency check, competitor/market validation,
        adapter resolution, deterministic request construction, and the
        PENDING AgentSession + its evidence-snapshot inputs -- all
        committed before any external call. Returns
        (agent_session_id, already_valid, adapter, request, batch); when
        already_valid is True, adapter/request/batch are None because a
        VALID session for this orchestration_key already existed and no
        new work was prepared.
        """

        with session_scope() as session:
            repo = AgentSessionRepository(session)
            existing = repo.find_by_orchestration_key(orchestration_key)
            if existing is not None and existing.status == "VALID":
                return existing.id, True, None, None, None

            season_competitor = self._require_competitor_in_season(session, season_competitor_id)
            competitor = session.get(Competitor, season_competitor.competitor_id)
            if competitor is None:
                raise LookupError(f"competitor {season_competitor.competitor_id} not found")

            adapter = self.registry.for_competitor(session, season_competitor_id)

            batch = self._build_market_batch(session, evidence_snapshot_ids)
            request = BenchmarkForecastRequest(
                task_type=CALL_TYPE_BENCHMARK_FORECASTING,
                prompt_version=BENCHMARK_PROMPT_VERSION,
                schema_version=FORECAST_SCHEMA_VERSION,
                season=batch.season,
                week=batch.week,
                checkpoint=batch.checkpoint,
                markets=batch.sorted_inputs(),
            )
            rendered_request = render_benchmark_prompt(request)

            bankroll_at_decision_cents = LedgerRepository(session).available_balance(season_competitor_id).cents

            agent_session = repo.create_pending(
                season_competitor_id=season_competitor_id,
                provider=competitor.provider,
                model_identifier=season_competitor.model_identifier,
                call_type=CALL_TYPE_BENCHMARK_FORECASTING,
                prompt_version=BENCHMARK_PROMPT_VERSION,
                schema_version=FORECAST_SCHEMA_VERSION,
                orchestration_key=orchestration_key,
                rendered_request=rendered_request,
                bankroll_at_decision_cents=bankroll_at_decision_cents,
                evidence_snapshot_ids_by_market=batch.evidence_snapshot_ids_by_market(),
            )
            return agent_session.id, False, adapter, request, batch

    def _call_with_corrections(
        self, adapter, request: BenchmarkForecastRequest
    ) -> tuple[ProviderCallResult, ValidationResult | None, int, int]:
        """Attempt 1 is the normal call; on schema-invalid content, retry
        up to `max_correction_retries` more times. Transport failures
        (no usable payload at all) are terminal immediately -- they are
        not correction-retried here, since there is nothing to correct.
        transport_retry_count stays 0 in Phase 3A (no automatic
        transport-level retry loop is implemented yet); the field exists
        so that behavior can be added later without another migration.
        """

        max_attempts = self.max_correction_retries + 1
        transport_retry_count = 0
        for attempt_number in range(1, max_attempts + 1):
            result = self._invoke_adapter(adapter, request)
            if result.error is not None:
                return result, None, transport_retry_count, attempt_number - 1
            validation = validate_benchmark_response(request, result.parsed_payload)
            if validation.is_valid or attempt_number == max_attempts:
                return result, validation, transport_retry_count, attempt_number - 1
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _invoke_adapter(adapter, request: BenchmarkForecastRequest) -> ProviderCallResult:
        """Backstop around every adapter call: an adapter is expected to
        catch its own provider's exceptions and return a normalized
        ProviderCallResult, but a gap in that handling (a raw exception
        type the adapter didn't anticipate) must never leave an
        AgentSession stuck at CALLING forever -- it must still reach a
        terminal FAILED state. This is defense in depth, not a substitute
        for correct per-adapter error handling.
        """

        try:
            return adapter.forecast_benchmark(request)
        except Exception as exc:  # noqa: BLE001 -- intentional catch-all backstop
            return ProviderCallResult(
                provider=getattr(adapter, "provider_name", "unknown"),
                model_identifier=getattr(adapter, "model_identifier", "unknown"),
                raw_response=None,
                parsed_payload=None,
                provider_request_id=None,
                usage_metadata=None,
                error=ProviderError(
                    category="UNKNOWN_PROVIDER_ERROR",
                    message=f"adapter raised an unhandled {type(exc).__name__}: {exc}",
                ),
            )

    def _require_competitor_in_season(self, session: Session, season_competitor_id: uuid.UUID) -> SeasonCompetitor:
        season_competitor = session.get(SeasonCompetitor, season_competitor_id)
        if season_competitor is None:
            raise LookupError(f"season_competitor {season_competitor_id} not found")
        if season_competitor.season_id != self.season_id:
            raise CrossSeasonReference(
                f"season_competitor {season_competitor_id} belongs to season {season_competitor.season_id}, "
                f"not this orchestrator's season {self.season_id}"
            )
        return season_competitor

    def _build_market_batch(self, session: Session, evidence_snapshot_ids: list[uuid.UUID]) -> "_ResolvedBatch":
        if not evidence_snapshot_ids:
            raise ValueError("evidence_snapshot_ids must not be empty")

        market_repo = MarketRepository(session)
        batch = _ResolvedBatch()
        season_years: set[int] = set()
        week_numbers: set[int] = set()

        for evidence_snapshot_id in evidence_snapshot_ids:
            evidence = get_evidence_snapshot(session, evidence_snapshot_id)
            market_snapshot = market_repo.get_market_snapshot(evidence.market_snapshot_id)
            prop_market = market_repo.get_prop_market(evidence.market_id)
            game = market_repo.get_game(prop_market.game_id)
            player = session.get(Player, prop_market.player_id)
            if player is None:
                raise LookupError(f"player {prop_market.player_id} not found")

            if game.season_id != self.season_id:
                raise CrossSeasonReference(
                    f"evidence_snapshot {evidence_snapshot_id} belongs to season {game.season_id} "
                    f"(via game {game.id}), not this orchestrator's season {self.season_id}"
                )

            if not market_snapshot.is_valid_canonical_baseline or market_snapshot.canonical_line is None:
                raise ValueError(
                    f"market_snapshot {market_snapshot.id} has no valid canonical baseline; "
                    "cannot build a benchmark request from it"
                )
            if (
                market_snapshot.canonical_over_probability is None
                or market_snapshot.market_median_line is None
                or market_snapshot.market_min_line is None
                or market_snapshot.market_max_line is None
            ):
                raise ValueError(f"market_snapshot {market_snapshot.id} is missing required market-context fields")

            season = self._season_year(session)
            season_years.add(season)
            week_numbers.add(game.week_number)

            opponent = game.away_team if player.team == game.home_team else game.home_team
            market_input = MarketInput(
                market_id=str(prop_market.id),
                player=player.name,
                team=player.team,
                opponent=opponent,
                stat_type=prop_market.stat_type,
                canonical_line=market_snapshot.canonical_line,
                canonical_over_price=market_snapshot.canonical_over_price,
                canonical_under_price=market_snapshot.canonical_under_price,
                canonical_market_probability_over=market_snapshot.canonical_over_probability,
                same_line_consensus_probability_over=market_snapshot.same_line_consensus_over_probability,
                market_context=MarketContext(
                    median_line=market_snapshot.market_median_line,
                    min_line=market_snapshot.market_min_line,
                    max_line=market_snapshot.market_max_line,
                    books=market_snapshot.number_of_books,
                ),
                evidence=evidence.payload,
            )
            batch.add(market_input, prop_market.id, evidence_snapshot_id)

        if len(season_years) != 1 or len(week_numbers) != 1:
            raise ValueError(
                "all evidence_snapshot_ids in one benchmark call must belong to the same season/week; "
                f"got seasons={season_years}, weeks={week_numbers}"
            )
        batch.season = season_years.pop()
        batch.week = week_numbers.pop()
        # Every evidence_snapshot in a batch is expected to share one
        # checkpoint window (e.g. all OPENING); take it from the first
        # snapshot rather than assuming a fixed literal.
        first_evidence = get_evidence_snapshot(session, evidence_snapshot_ids[0])
        batch.checkpoint = first_evidence.checkpoint_type or "OPENING"
        return batch

    def _season_year(self, session: Session) -> int:
        from app.db.models.season import Season

        season = session.get(Season, self.season_id)
        if season is None:
            raise LookupError(f"season {self.season_id} not found")
        return season.year


class _ResolvedBatch(_MarketBatch):
    season: int
    week: int
    checkpoint: str
