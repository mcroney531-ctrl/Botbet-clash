"""Phase 1 domain skeleton (constitution §117): Season, SeasonRules, Week,
Competitor, bankroll ledger entries, Ticket, Wager, Settlement,
CompetitionEvent — plus a minimal FakePropMarket so the whole weekly
lifecycle is exercisable without a real sports-data provider (that's
Phase 2/6). These are plain dataclasses, not ORM rows: persistence
(SQLAlchemy models matching DATABASE.md) is a Phase 2+ concern layered on
top of this module, not a rewrite of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.ids import new_id
from app.core.money import Money
from app.domain.enums import (
    BankrollTransactionType,
    CompetitionEventType,
    CompetitorStatus,
    RiskPosture,
    SeasonStatus,
    Side,
    SportsbookResult,
    TicketStatus,
    Uncertainty,
    Urgency,
    WagerExecutionStatus,
    WeekStatus,
)


@dataclass(frozen=True, slots=True)
class SeasonRules:
    """Versioned, immutable season configuration (RULES.md).

    A rule change mid-season never mutates this object — a new
    `SeasonRules` with a new `rules_version` is created and
    `Season.rules_version` is repointed to it (see §14 of RULES.md).
    """

    rules_version: str
    starting_bankroll: Money
    kelly_fraction: Decimal
    standard_max_bankroll_fraction: Decimal
    exceptional_max_bankroll_fraction: Decimal
    minimum_stake: Money
    stake_increment: Money
    pounce_limit: int = 1

    def standard_cap(self, available_bankroll: Money) -> Money:
        return available_bankroll.fraction(self.standard_max_bankroll_fraction)

    def exceptional_cap(self, available_bankroll: Money) -> Money:
        return available_bankroll.fraction(self.exceptional_max_bankroll_fraction)

    def cap_for(self, available_bankroll: Money, urgency: Urgency) -> Money:
        if urgency is Urgency.POUNCE:
            return self.exceptional_cap(available_bankroll)
        return self.standard_cap(available_bankroll)


@dataclass(slots=True)
class Season:
    name: str
    year: int
    rules: SeasonRules
    id: str = field(default_factory=new_id)
    status: SeasonStatus = SeasonStatus.DRAFT


@dataclass(slots=True)
class Week:
    season_id: str
    week_number: int
    is_real_money: bool
    id: str = field(default_factory=new_id)
    status: WeekStatus = WeekStatus.PENDING
    opened_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(slots=True)
class Competitor:
    id: str
    provider: str
    model_identifier: str
    model_version: str
    status: CompetitorStatus = CompetitorStatus.ACTIVE
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FakePropMarket:
    """Stand-in for `PropMarket`/`PropQuote` (Phase 2). Enough shape to
    drive a ticket → wager → settlement lifecycle end to end."""

    id: str
    description: str
    line: Decimal
    over_price: int
    under_price: int


@dataclass(slots=True)
class BankrollTransaction:
    competitor_id: str
    type: BankrollTransactionType
    amount: Money
    id: str = field(default_factory=new_id)
    week_id: str | None = None
    wager_id: str | None = None
    reason: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class Ticket:
    competitor_id: str
    week_id: str
    market_id: str
    side: Side
    urgency: Urgency
    risk_posture: RiskPosture
    kelly_reference_stake: Money
    model_requested_stake: Money
    final_allowed_stake: Money
    observed_line: Decimal
    why_now: str
    id: str = field(default_factory=new_id)
    acceptable_line_boundary: Decimal | None = None
    maximum_acceptable_price: int | None = None
    valid_until: datetime | None = None
    status: TicketStatus = TicketStatus.ISSUED
    created_at: datetime | None = None


@dataclass(slots=True)
class Wager:
    ticket_id: str
    competitor_id: str
    week_id: str
    market_id: str
    requested_stake: Money
    id: str = field(default_factory=new_id)
    execution_status: WagerExecutionStatus | None = None
    sportsbook: str | None = None
    actual_line: Decimal | None = None
    actual_price: int | None = None
    actual_stake: Money | None = None
    execution_timestamp: datetime | None = None
    bankroll_at_execution: Money | None = None


@dataclass(slots=True)
class Settlement:
    wager_id: str
    result: SportsbookResult
    payout: Money
    id: str = field(default_factory=new_id)
    settled_at: datetime | None = None


@dataclass(slots=True)
class PassDecision:
    competitor_id: str
    week_id: str
    reason_for_pass: str
    id: str = field(default_factory=new_id)
    best_available_candidate: str | None = None
    estimated_edge: Decimal | None = None
    confidence: Decimal | None = None
    uncertainty: Uncertainty | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class CompetitionEvent:
    season_id: str
    event_type: CompetitionEventType
    id: str = field(default_factory=new_id)
    week_id: str | None = None
    competitor_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime | None = None
