"""Research settlement (DATABASE.md §8's v2 fix): persist only the final
stat value. Outcome is derived per-`ForecastObservation` against that
observation's own `canonical_line` — never a stored market-level
OVER/UNDER, because Opening and Final can legitimately be forecast
against different lines for the same market.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models.settlement import ResearchSettlement
from app.db.repositories.research_repository import ResearchRepository


def lock_research_settlement(
    session: Session, *, market_id: uuid.UUID, stat_value: Decimal, locked_at: datetime
) -> ResearchSettlement:
    row = ResearchSettlement(market_id=market_id, research_stat_value_at_lock=stat_value, research_locked_at=locked_at)
    return ResearchRepository(session).lock(row)


def apply_later_correction(session: Session, *, market_id: uuid.UUID, corrected_stat: Decimal, correction_timestamp: datetime) -> ResearchSettlement:
    row = ResearchRepository(session).get_for_market(market_id)
    if row is None:
        raise LookupError(f"no research_settlement for market {market_id} to correct")
    row.later_corrected_stat = corrected_stat
    row.correction_timestamp = correction_timestamp
    session.flush()
    return row


def derive_outcome(stat_value: Decimal, canonical_line: Decimal) -> str:
    """RULES.md §34: outcome is defined identically for model and market,
    always relative to *this observation's own* line."""

    if stat_value > canonical_line:
        return "OVER"
    if stat_value < canonical_line:
        return "UNDER"
    raise ValueError(
        f"stat_value {stat_value} exactly equals canonical_line {canonical_line} - "
        "this can only happen for a pushable line, which should already be research_eligible=False"
    )
