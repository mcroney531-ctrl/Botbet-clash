"""ForecastObservation creation (RULES.md §23-24). One function for both
BENCHMARK and OPEN_MARKET observations; revisions are always a fresh
INSERT — this module has no update path at all, by construction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models.forecast_lab import ForecastObservation
from app.db.models.markets import MarketSnapshot
from app.db.repositories.forecast_repository import ForecastRepository
from app.db.repositories.market_repository import MarketRepository
from app.domain.enums import Uncertainty
from app.domain.lines import is_pushable_line
from app.forecast_lab.evidence_service import get_evidence_snapshot


def _research_eligibility(market_snapshot: MarketSnapshot) -> tuple[bool, str | None]:
    if not market_snapshot.is_valid_canonical_baseline:
        return False, "CANONICAL_MARKET_UNAVAILABLE"
    if market_snapshot.canonical_line is None:
        return False, "TWO_SIDED_PRICE_UNAVAILABLE"
    if is_pushable_line(market_snapshot.canonical_line):
        return False, "RESEARCH_INELIGIBLE_PUSHABLE_LINE"
    return True, None


def create_forecast_observation(
    session: Session,
    *,
    season_competitor_id: uuid.UUID,
    market_id: uuid.UUID,
    source_type: str,  # BENCHMARK | OPEN_MARKET
    checkpoint_type: str | None,
    timestamp: datetime,
    model_probability_over: Decimal,
    evidence_snapshot_id: uuid.UUID,
    confidence: Decimal,
    uncertainty: Uncertainty,
    agent_session_id: uuid.UUID | None = None,
    revision_parent_id: uuid.UUID | None = None,
    revision_reason: str | None = None,
) -> ForecastObservation:
    """Takes `evidence_snapshot_id` only — never a caller-supplied
    `MarketSnapshot` alongside it. Accepting both independently would
    let a caller give two competitors the same evidence_snapshot_id
    while quietly stamping each observation's canonical_line/prices from
    a *different* MarketSnapshot object, which the cohort's shared-
    evidence-snapshot check (cohort.py) has no way to catch — it only
    ever sees the id, not what it actually points to. Loading the
    evidence row here and deriving the market snapshot from
    `evidence.market_snapshot_id` makes that mismatch structurally
    impossible instead of relying on the caller to pass consistent
    arguments.
    """

    evidence = get_evidence_snapshot(session, evidence_snapshot_id)
    if evidence.market_id != market_id:
        raise ValueError(
            f"evidence_snapshot {evidence_snapshot_id} belongs to market {evidence.market_id}, not {market_id}"
        )
    market_snapshot = MarketRepository(session).get_market_snapshot(evidence.market_snapshot_id)

    research_eligible, exclusion_reason = _research_eligibility(market_snapshot)
    canonical_prob = market_snapshot.canonical_over_probability
    disagreement = (model_probability_over - canonical_prob) if canonical_prob is not None else None

    row = ForecastObservation(
        season_competitor_id=season_competitor_id,
        market_id=market_id,
        source_type=source_type,
        checkpoint_type=checkpoint_type,
        timestamp=timestamp,
        model_probability_over=model_probability_over,
        canonical_market_probability_over=canonical_prob,
        same_line_consensus_probability_over=market_snapshot.same_line_consensus_over_probability,
        canonical_line=market_snapshot.canonical_line,
        canonical_over_price=market_snapshot.canonical_over_price,
        canonical_under_price=market_snapshot.canonical_under_price,
        market_median_line=market_snapshot.market_median_line,
        probability_disagreement=disagreement,
        confidence=confidence,
        uncertainty=uncertainty.value,
        evidence_snapshot_id=evidence_snapshot_id,
        revision_parent_id=revision_parent_id,
        revision_reason=revision_reason,
        research_eligible=research_eligible,
        exclusion_reason=exclusion_reason,
        agent_session_id=agent_session_id,
    )
    ForecastRepository(session).add(row)
    return row
