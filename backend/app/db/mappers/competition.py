"""Pure domain <-> ORM translation. No session access, no business logic —
just field-by-field conversion (Money.cents <-> int, enum <-> str, etc).
Business rules live in `app/domain/` (risk math, validation) and
`app/services/` (orchestration); this module never imports either.
"""

from __future__ import annotations

from app.core.money import Money
from app.db.models.competition import PassDecision as PassDecisionRow
from app.db.models.competition import Ticket as TicketRow
from app.db.models.competition import Wager as WagerRow
from app.db.models.events import CompetitionEvent as CompetitionEventRow
from app.db.models.season import Competitor as CompetitorRow
from app.db.models.season import Season as SeasonRow
from app.db.models.season import SeasonCompetitor as SeasonCompetitorRow
from app.db.models.season import SeasonRules as SeasonRulesRow
from app.db.models.season import Week as WeekRow
from app.db.models.settlement import BankrollTransaction as BankrollTransactionRow
from app.db.models.settlement import Settlement as SettlementRow
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
from app.domain.models import (
    BankrollTransaction,
    Competitor,
    CompetitionEvent,
    PassDecision,
    Season,
    SeasonRules,
    Settlement,
    Ticket,
    Wager,
    Week,
)


def season_rules_to_domain(row: SeasonRulesRow) -> SeasonRules:
    return SeasonRules(
        rules_version=row.rules_version,
        starting_bankroll=Money(row.starting_bankroll_cents),
        kelly_fraction=row.kelly_fraction,
        standard_max_bankroll_fraction=row.standard_max_bankroll_fraction,
        exceptional_max_bankroll_fraction=row.exceptional_max_bankroll_fraction,
        minimum_stake=Money(row.minimum_stake_cents),
        stake_increment=Money(row.stake_increment_cents),
        pounce_limit=row.pounce_limit,
    )


def season_to_domain(row: SeasonRow, rules_row: SeasonRulesRow) -> Season:
    return Season(
        id=str(row.id),
        name=row.name,
        year=row.year,
        rules=season_rules_to_domain(rules_row),
        status=SeasonStatus(row.status),
    )


def week_to_domain(row: WeekRow) -> Week:
    return Week(
        id=str(row.id),
        season_id=str(row.season_id),
        week_number=row.week_number,
        is_real_money=row.is_real_money,
        status=WeekStatus(row.status),
        opened_at=row.opened_at,
        closed_at=row.closed_at,
    )


def competitor_to_domain(season_competitor_row: SeasonCompetitorRow, competitor_row: CompetitorRow) -> Competitor:
    return Competitor(
        id=str(season_competitor_row.id),
        provider=competitor_row.provider,
        model_identifier=season_competitor_row.model_identifier,
        model_version=season_competitor_row.model_version,
        status=CompetitorStatus(season_competitor_row.status),
        provider_metadata=dict(season_competitor_row.provider_metadata or {}),
    )


def ticket_to_orm(ticket: Ticket, market_uuid) -> TicketRow:
    return TicketRow(
        id=ticket.id,
        season_competitor_id=ticket.competitor_id,
        week_id=ticket.week_id,
        market_id=market_uuid,
        urgency=ticket.urgency.value,
        side=ticket.side.value,
        risk_posture=ticket.risk_posture.value,
        kelly_reference_stake_cents=ticket.kelly_reference_stake.cents,
        model_requested_stake_cents=ticket.model_requested_stake.cents,
        final_allowed_stake_cents=ticket.final_allowed_stake.cents,
        observed_line=ticket.observed_line,
        acceptable_line_boundary=ticket.acceptable_line_boundary,
        worst_acceptable_price=ticket.worst_acceptable_price,
        why_now=ticket.why_now,
        valid_until=ticket.valid_until,
        status=ticket.status.value,
        created_at=ticket.created_at,
    )


def ticket_to_domain(row: TicketRow) -> Ticket:
    return Ticket(
        competitor_id=str(row.season_competitor_id),
        week_id=str(row.week_id),
        market_id=str(row.market_id),
        side=Side(row.side),
        urgency=Urgency(row.urgency),
        risk_posture=RiskPosture(row.risk_posture),
        kelly_reference_stake=Money(row.kelly_reference_stake_cents),
        model_requested_stake=Money(row.model_requested_stake_cents),
        final_allowed_stake=Money(row.final_allowed_stake_cents),
        observed_line=row.observed_line,
        why_now=row.why_now,
        id=str(row.id),
        acceptable_line_boundary=row.acceptable_line_boundary,
        worst_acceptable_price=row.worst_acceptable_price,
        valid_until=row.valid_until,
        status=TicketStatus(row.status),
        created_at=row.created_at,
    )


def wager_to_orm(wager: Wager, market_uuid) -> WagerRow:
    return WagerRow(
        id=wager.id,
        ticket_id=wager.ticket_id,
        season_competitor_id=wager.competitor_id,
        week_id=wager.week_id,
        market_id=market_uuid,
        requested_stake_cents=wager.requested_stake.cents,
        execution_status=wager.execution_status.value,
        sportsbook=wager.sportsbook,
        actual_line=wager.actual_line,
        actual_price=wager.actual_price,
        actual_stake_cents=(wager.actual_stake.cents if wager.actual_stake is not None else None),
        execution_timestamp=wager.execution_timestamp,
        bankroll_at_execution_cents=(
            wager.bankroll_at_execution.cents if wager.bankroll_at_execution is not None else None
        ),
    )


def wager_to_domain(row: WagerRow) -> Wager:
    return Wager(
        ticket_id=str(row.ticket_id),
        competitor_id=str(row.season_competitor_id),
        week_id=str(row.week_id),
        market_id=str(row.market_id),
        requested_stake=Money(row.requested_stake_cents),
        id=str(row.id),
        execution_status=WagerExecutionStatus(row.execution_status),
        sportsbook=row.sportsbook,
        actual_line=row.actual_line,
        actual_price=row.actual_price,
        actual_stake=(Money(row.actual_stake_cents) if row.actual_stake_cents is not None else None),
        execution_timestamp=row.execution_timestamp,
        bankroll_at_execution=(
            Money(row.bankroll_at_execution_cents) if row.bankroll_at_execution_cents is not None else None
        ),
    )


def pass_decision_to_orm(decision: PassDecision, best_candidate_market_uuid) -> PassDecisionRow:
    return PassDecisionRow(
        id=decision.id,
        season_competitor_id=decision.competitor_id,
        week_id=decision.week_id,
        best_available_candidate_market_id=best_candidate_market_uuid,
        estimated_edge=decision.estimated_edge,
        confidence=decision.confidence,
        uncertainty=(decision.uncertainty.value if decision.uncertainty is not None else None),
        reason_for_pass=decision.reason_for_pass,
        created_at=decision.created_at,
    )


def pass_decision_to_domain(row: PassDecisionRow) -> PassDecision:
    return PassDecision(
        competitor_id=str(row.season_competitor_id),
        week_id=str(row.week_id),
        reason_for_pass=row.reason_for_pass,
        id=str(row.id),
        best_available_candidate=(str(row.best_available_candidate_market_id) if row.best_available_candidate_market_id else None),
        estimated_edge=row.estimated_edge,
        confidence=row.confidence,
        uncertainty=(Uncertainty(row.uncertainty) if row.uncertainty else None),
        created_at=row.created_at,
    )


def settlement_to_orm(settlement: Settlement) -> SettlementRow:
    return SettlementRow(
        id=settlement.id,
        wager_id=settlement.wager_id,
        sportsbook_result=settlement.result.value,
        sportsbook_payout_cents=settlement.payout.cents,
        sportsbook_settled_at=settlement.settled_at,
    )


def settlement_to_domain(row: SettlementRow) -> Settlement:
    return Settlement(
        wager_id=str(row.wager_id),
        result=SportsbookResult(row.sportsbook_result),
        payout=Money(row.sportsbook_payout_cents),
        id=str(row.id),
        settled_at=row.sportsbook_settled_at,
    )


def bankroll_transaction_to_orm(txn: BankrollTransaction) -> BankrollTransactionRow:
    return BankrollTransactionRow(
        id=txn.id,
        season_competitor_id=txn.competitor_id,
        week_id=txn.week_id,
        wager_id=txn.wager_id,
        type=txn.type.value,
        amount_cents=txn.amount.cents,
        reason=txn.reason,
        created_at=txn.created_at,
    )


def bankroll_transaction_to_domain(row: BankrollTransactionRow) -> BankrollTransaction:
    return BankrollTransaction(
        competitor_id=str(row.season_competitor_id),
        type=BankrollTransactionType(row.type),
        amount=Money(row.amount_cents),
        id=str(row.id),
        week_id=(str(row.week_id) if row.week_id else None),
        wager_id=(str(row.wager_id) if row.wager_id else None),
        reason=row.reason,
        created_at=row.created_at,
    )


def competition_event_to_orm(event: CompetitionEvent) -> CompetitionEventRow:
    return CompetitionEventRow(
        id=event.id,
        timestamp=event.timestamp,
        season_id=event.season_id,
        week_id=event.week_id,
        season_competitor_id=event.competitor_id,
        event_type=event.event_type.value,
        payload=event.payload,
    )


def competition_event_to_domain(row: CompetitionEventRow) -> CompetitionEvent:
    return CompetitionEvent(
        season_id=str(row.season_id),
        event_type=CompetitionEventType(row.event_type),
        id=str(row.id),
        week_id=(str(row.week_id) if row.week_id else None),
        competitor_id=(str(row.season_competitor_id) if row.season_competitor_id else None),
        payload=dict(row.payload or {}),
        timestamp=row.timestamp,
    )
