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
from app.domain.enums import Uncertainty
from app.domain.lines import is_pushable_line


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
    market_snapshot: MarketSnapshot,
    evidence_snapshot_id: uuid.UUID,
    confidence: Decimal,
    uncertainty: Uncertainty,
    agent_session_id: uuid.UUID | None = None,
    revision_parent_id: uuid.UUID | None = None,
    revision_reason: str | None = None,
) -> ForecastObservation:
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
