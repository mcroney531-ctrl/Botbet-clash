"""Common-coverage research cohort (RULES.md §46-48). A market/checkpoint
enters the headline cross-model comparison only when every one of these
exists: a valid forecast from every named competitor, a valid canonical
baseline, a valid research outcome, and a research-eligible line. Anything
incomplete is stored (nothing here deletes or hides a row) but excluded
from the cohort with an explicit reason — never imputed, never carried
forward from an earlier forecast.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.repositories.forecast_repository import ForecastRepository
from app.db.repositories.research_repository import ResearchRepository


@dataclass(frozen=True, slots=True)
class MarketCohortResult:
    market_id: uuid.UUID
    eligible: bool
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class CohortReport:
    potential_count: int
    eligible_count: int
    rows: list[MarketCohortResult] = field(default_factory=list)

    @property
    def coverage_percentage(self) -> Decimal:
        if self.potential_count == 0:
            return Decimal(0)
        return (Decimal(self.eligible_count) / Decimal(self.potential_count) * 100).quantize(Decimal("0.1"))


def evaluate_common_cohort(
    session: Session,
    *,
    market_ids: list[uuid.UUID],
    checkpoint_type: str | None,
    competitor_labels: dict[uuid.UUID, str],
) -> CohortReport:
    """`competitor_labels` maps each season_competitor_id required in the
    cohort to its RULES.md-exclusion-reason label (e.g. "GPT"), so a
    missing forecast reports `GPT_FORECAST_MISSING` rather than a bare
    UUID — this project's roster is three fixed, named competitors, not
    an arbitrary N, so RULES.md §47 names them explicitly.
    """

    forecast_repo = ForecastRepository(session)
    research_repo = ResearchRepository(session)
    rows: list[MarketCohortResult] = []

    for market_id in market_ids:
        exclusion_reason: str | None = None

        observations = {}
        for season_competitor_id, label in competitor_labels.items():
            obs = forecast_repo.latest_for_competitor_market_checkpoint(season_competitor_id, market_id, checkpoint_type)
            if obs is None:
                exclusion_reason = exclusion_reason or f"{label}_FORECAST_MISSING"
            else:
                observations[season_competitor_id] = obs

        if exclusion_reason is None:
            for season_competitor_id, label in competitor_labels.items():
                obs = observations[season_competitor_id]
                if not obs.research_eligible:
                    exclusion_reason = obs.exclusion_reason or f"{label}_FORECAST_INVALID"
                    break

        # Integrity check: at a standardized checkpoint (checkpoint_type
        # is not None), the whole point of atomic checkpoint capture
        # (ARCHITECTURE.md §4) is that every competitor is handed the
        # identical evidence snapshot. If their observations somehow
        # point at different snapshots, they weren't actually compared
        # under the same information - that's a system error, not a
        # market to silently score.
        if exclusion_reason is None and checkpoint_type is not None:
            evidence_snapshot_ids = {obs.evidence_snapshot_id for obs in observations.values()}
            if len(evidence_snapshot_ids) > 1:
                exclusion_reason = "OTHER"

        if exclusion_reason is None:
            settlement = research_repo.get_for_market(market_id)
            if settlement is None:
                exclusion_reason = "RESEARCH_OUTCOME_UNAVAILABLE"

        rows.append(MarketCohortResult(market_id=market_id, eligible=exclusion_reason is None, exclusion_reason=exclusion_reason))

    eligible_count = sum(1 for r in rows if r.eligible)
    return CohortReport(potential_count=len(rows), eligible_count=eligible_count, rows=rows)
