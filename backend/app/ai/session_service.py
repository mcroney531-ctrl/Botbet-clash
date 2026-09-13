"""AgentSession persistence: the auditable record of every consequential
model call. Two-transaction lifecycle (ARCHITECTURE.md's "no DB
transaction spans an external call" rule): callers create a PENDING row
and commit *before* calling a provider, then come back in a fresh
transaction to record CALLING -> VALID/INVALID/FAILED.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models.forecast_lab import AgentSession, AgentSessionEvidenceSnapshot
from app.db.models.season import Competitor, SeasonCompetitor


def build_orchestration_key(
    *, season_competitor_id: uuid.UUID, checkpoint_run_id: uuid.UUID, call_type: str, prompt_version: str
) -> str:
    """Deterministic logical-call key so a rerun of the same orchestration
    job can detect an already-completed valid session instead of creating
    a second one (idempotency; brief's "don't charge a provider twice
    because a worker restarted after receiving the response")."""

    return f"{season_competitor_id}:{checkpoint_run_id}:{call_type}:{prompt_version}"


class AgentSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, agent_session_id: uuid.UUID) -> AgentSession:
        row = self.session.get(AgentSession, agent_session_id)
        if row is None:
            raise LookupError(f"agent_session {agent_session_id} not found")
        return row

    def find_by_orchestration_key(self, orchestration_key: str) -> AgentSession | None:
        return (
            self.session.query(AgentSession)
            .filter(AgentSession.orchestration_key == orchestration_key)
            .one_or_none()
        )

    def create_pending(
        self,
        *,
        season_competitor_id: uuid.UUID,
        provider: str,
        model_identifier: str,
        call_type: str,
        prompt_version: str,
        schema_version: str,
        orchestration_key: str,
        rendered_request: dict,
        bankroll_at_decision_cents: int,
        evidence_snapshot_ids_by_market: dict[uuid.UUID, uuid.UUID],
    ) -> AgentSession:
        """Create and flush a PENDING AgentSession plus its
        agent_session_evidence_snapshots rows. `evidence_snapshot_ids_by_market`
        maps market_id -> evidence_snapshot_id for every input in this call
        -- the join table, not a singular column, is the source of truth
        for multi-market/batch inputs.
        """

        now = datetime.now(timezone.utc)
        row = AgentSession(
            season_competitor_id=season_competitor_id,
            provider=provider,
            model_identifier=model_identifier,
            call_type=call_type,
            prompt_version=prompt_version,
            schema_version=schema_version,
            status="PENDING",
            orchestration_key=orchestration_key,
            rendered_request=rendered_request,
            bankroll_at_decision_cents=bankroll_at_decision_cents,
            timestamp=now,
        )
        self.session.add(row)
        self.session.flush()

        for market_id, evidence_snapshot_id in evidence_snapshot_ids_by_market.items():
            self.session.add(
                AgentSessionEvidenceSnapshot(
                    agent_session_id=row.id,
                    evidence_snapshot_id=evidence_snapshot_id,
                    market_id=market_id,
                )
            )
        self.session.flush()
        return row

    def mark_calling(self, agent_session: AgentSession) -> None:
        agent_session.status = "CALLING"
        self.session.flush()

    def mark_valid(
        self,
        agent_session: AgentSession,
        *,
        raw_response: object,
        validated_response: dict,
        provider_request_id: str | None,
        usage_metadata: dict | None,
        transport_retry_count: int,
        correction_retry_count: int,
    ) -> None:
        agent_session.status = "VALID"
        agent_session.raw_response = raw_response
        agent_session.validated_response = validated_response
        agent_session.is_valid = True
        agent_session.provider_request_id = provider_request_id
        agent_session.usage_metadata = usage_metadata
        agent_session.transport_retry_count = transport_retry_count
        agent_session.correction_retry_count = correction_retry_count
        agent_session.retry_count = transport_retry_count + correction_retry_count
        agent_session.completed_at = datetime.now(timezone.utc)
        self.session.flush()

    def mark_invalid(
        self,
        agent_session: AgentSession,
        *,
        raw_response: object,
        error_category: str,
        error_message: str,
        transport_retry_count: int,
        correction_retry_count: int,
    ) -> None:
        """The provider responded, but the response never passed content
        validation even after the bounded correction retry."""

        agent_session.status = "INVALID"
        agent_session.raw_response = raw_response
        agent_session.validated_response = None
        agent_session.is_valid = False
        agent_session.error_category = error_category
        agent_session.error_message = error_message
        agent_session.transport_retry_count = transport_retry_count
        agent_session.correction_retry_count = correction_retry_count
        agent_session.retry_count = transport_retry_count + correction_retry_count
        agent_session.completed_at = datetime.now(timezone.utc)
        self.session.flush()

    def mark_failed(
        self,
        agent_session: AgentSession,
        *,
        error_category: str,
        error_message: str,
        transport_retry_count: int,
    ) -> None:
        """No usable answer was ever obtained (transport failure exhausted
        its retries) -- distinct from INVALID, where the provider did
        respond but failed content validation."""

        agent_session.status = "FAILED"
        agent_session.is_valid = False
        agent_session.error_category = error_category
        agent_session.error_message = error_message
        agent_session.transport_retry_count = transport_retry_count
        agent_session.retry_count = transport_retry_count
        agent_session.completed_at = datetime.now(timezone.utc)
        self.session.flush()

    def evidence_snapshot_ids(self, agent_session_id: uuid.UUID) -> list[uuid.UUID]:
        rows = (
            self.session.query(AgentSessionEvidenceSnapshot)
            .filter(AgentSessionEvidenceSnapshot.agent_session_id == agent_session_id)
            .all()
        )
        return [row.evidence_snapshot_id for row in rows]


def build_audit_receipt(session: Session, agent_session_id: uuid.UUID) -> dict:
    """Reproducibility receipt for one AgentSession (brief's illustrative
    JSON shape) -- enough to point at one call and say exactly what a
    competitor saw, what it returned, and what got persisted from it."""

    from app.db.models.forecast_lab import ForecastObservation

    repo = AgentSessionRepository(session)
    agent_session = repo.get(agent_session_id)
    season_competitor = session.get(SeasonCompetitor, agent_session.season_competitor_id)
    if season_competitor is None:
        raise LookupError(f"season_competitor {agent_session.season_competitor_id} not found")
    competitor = session.get(Competitor, season_competitor.competitor_id)
    if competitor is None:
        raise LookupError(f"competitor {season_competitor.competitor_id} not found")

    forecast_observations = (
        session.query(ForecastObservation)
        .filter(ForecastObservation.agent_session_id == agent_session_id)
        .all()
    )

    return {
        "agent_session_id": str(agent_session.id),
        "competitor": competitor.display_name,
        "provider": agent_session.provider,
        "model_identifier": agent_session.model_identifier,
        "call_type": agent_session.call_type,
        "prompt_version": agent_session.prompt_version,
        "schema_version": agent_session.schema_version,
        "status": agent_session.status,
        "timestamp": agent_session.timestamp.isoformat(),
        "input_evidence_snapshot_ids": [str(eid) for eid in repo.evidence_snapshot_ids(agent_session_id)],
        "raw_response_preserved": agent_session.raw_response is not None,
        "validated": agent_session.is_valid is True,
        "transport_retry_count": agent_session.transport_retry_count,
        "correction_retry_count": agent_session.correction_retry_count,
        "forecast_observation_ids": [str(obs.id) for obs in forecast_observations],
    }
