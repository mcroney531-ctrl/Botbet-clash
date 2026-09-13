"""Phase 1 acceptance test: a full mocked weekly lifecycle for all three
competitors on fake markets — the goal stated in constitution §117 Phase 1
and exercised further (real providers, AI adapters) in later phases.
"""

from decimal import Decimal

import pytest

from app.core.money import Money
from app.domain.enums import (
    CompetitionEventType,
    CompetitorStatus,
    RiskPosture,
    Side,
    SportsbookResult,
    TicketStatus,
    Urgency,
    WagerExecutionStatus,
    WeekStatus,
)
from app.domain.errors import CompetitorBusted, DuplicateWeeklyDecision, PounceLimitExceeded
from app.domain.models import FakePropMarket, Week
from app.domain.season_service import SeasonService


def make_market(market_id: str = "mkt-1") -> FakePropMarket:
    return FakePropMarket(
        id=market_id,
        description="Player X Over/Under 52.5 receiving yards",
        line=Decimal("52.5"),
        over_price=-115,
        under_price=-105,
    )


def test_full_week_bet_win_and_pass(service: SeasonService, week1: Week):
    market = make_market()

    # openai: BET, wins.
    ticket = service.issue_ticket(
        week=week1,
        competitor=service._competitors["openai"],
        market=market,
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.61"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="Model sees value versus the canonical line.",
    )
    assert ticket.status is TicketStatus.ISSUED
    assert ticket.final_allowed_stake == Money.from_dollars_str("2.00")  # under the 20% cap of $15

    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert service.ledger.available_balance("openai") == Money.from_dollars_str("13.00")

    settlement = service.settle_wager(
        wager=wager,
        result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),  # stake back + profit at -115
    )
    assert settlement.result is SportsbookResult.WIN
    assert service.ledger.available_balance("openai") == Money.from_dollars_str("16.74")

    # anthropic: PASS, with structured reasoning.
    decision = service.record_pass(
        week=week1,
        competitor=service._competitors["anthropic"],
        reason_for_pass="Best candidate showed only a 1.8pt edge with high uncertainty.",
        best_available_candidate=market.id,
        estimated_edge=Decimal("0.018"),
        confidence=Decimal("5.5"),
    )
    assert decision.competitor_id == "anthropic"
    assert service.ledger.available_balance("anthropic") == Money.from_dollars_str("15.00")

    # google: BET, loses.
    ticket2 = service.issue_ticket(
        week=week1,
        competitor=service._competitors["google"],
        market=make_market("mkt-2"),
        side=Side.UNDER,
        urgency=Urgency.POUNCE,
        risk_posture=RiskPosture.AGGRESSIVE,
        model_probability_over=Decimal("0.35"),
        model_requested_stake=Money.from_dollars_str("4.00"),
        why_now="Line moved only slightly despite a material injury update.",
    )
    wager2 = service.record_execution(
        ticket=ticket2,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_line=Decimal("52.5"),
        actual_price=-105,
        actual_stake=Money.from_dollars_str("4.00"),
    )
    service.settle_wager(wager=wager2, result=SportsbookResult.LOSS, payout=Money.zero())
    assert service.ledger.available_balance("google") == Money.from_dollars_str("11.00")

    service.close_week(week1)
    assert week1.status is WeekStatus.CLOSED

    event_types = [e.event_type for e in service.event_bus.events_for_week(week1.id)]
    assert CompetitionEventType.WEEK_OPENED in event_types
    assert CompetitionEventType.BET_EXECUTED in event_types
    assert CompetitionEventType.PASS_DECLARED in event_types
    assert CompetitionEventType.POUNCE_ISSUED in event_types
    assert CompetitionEventType.PROP_WON in event_types
    assert CompetitionEventType.PROP_LOST in event_types
    assert CompetitionEventType.WEEK_CLOSED in event_types


def test_pounce_limit_enforced(service: SeasonService, week1: Week):
    competitor = service._competitors["openai"]
    service.issue_ticket(
        week=week1,
        competitor=competitor,
        market=make_market("mkt-1"),
        side=Side.OVER,
        urgency=Urgency.POUNCE,
        risk_posture=RiskPosture.AGGRESSIVE,
        model_probability_over=Decimal("0.65"),
        model_requested_stake=Money.from_dollars_str("3.00"),
        why_now="First pounce.",
    )
    with pytest.raises(PounceLimitExceeded):
        service.issue_ticket(
            week=week1,
            competitor=competitor,
            market=make_market("mkt-2"),
            side=Side.OVER,
            urgency=Urgency.POUNCE,
            risk_posture=RiskPosture.AGGRESSIVE,
            model_probability_over=Decimal("0.70"),
            model_requested_stake=Money.from_dollars_str("3.00"),
            why_now="Second pounce should be rejected.",
        )


def test_only_one_official_decision_per_week(service: SeasonService, week1: Week):
    competitor = service._competitors["openai"]
    ticket = service.issue_ticket(
        week=week1,
        competitor=competitor,
        market=make_market(),
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.60"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="Standard bet.",
    )
    service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_stake=Money.from_dollars_str("2.00"),
    )
    with pytest.raises(DuplicateWeeklyDecision):
        service.record_pass(week=week1, competitor=competitor, reason_for_pass="Too late, already bet.")


def test_bankruptcy_freezes_competitor(service: SeasonService, week1: Week):
    competitor = service._competitors["openai"]
    # Only one official decision is allowed per competitor per week, so
    # driving the bankroll down requires a fresh week each time (mirrors
    # how bankruptcy would actually unfold over a real season).
    week = week1
    for i in range(20):
        available = service.ledger.available_balance("openai")
        if available < service.season.rules.minimum_stake:
            break
        if i > 0:
            week = service.open_week(Week(season_id=service.season.id, week_number=i + 1, is_real_money=True))
        # Use the exceptional (Pounce) cap to drain the bankroll in a
        # reasonable number of iterations for the test.
        cap = service.season.rules.exceptional_cap(available)
        ticket = service.issue_ticket(
            week=week,
            competitor=competitor,
            market=make_market(f"mkt-{i}"),
            side=Side.OVER,
            urgency=Urgency.POUNCE,
            risk_posture=RiskPosture.AGGRESSIVE,
            model_probability_over=Decimal("0.60"),
            model_requested_stake=cap,
            why_now="Pressing to induce bankruptcy for the test.",
        )
        wager = service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            sportsbook="DRAFTKINGS",
            actual_stake=cap,
        )
        service.settle_wager(wager=wager, result=SportsbookResult.LOSS, payout=Money.zero())

    assert competitor.status is CompetitorStatus.BUSTED
    assert service.ledger.available_balance("openai") < service.season.rules.minimum_stake

    with pytest.raises(CompetitorBusted):
        service.issue_ticket(
            week=week1,
            competitor=competitor,
            market=make_market("mkt-after-bust"),
            side=Side.OVER,
            urgency=Urgency.STRONG,
            risk_posture=RiskPosture.STANDARD,
            model_probability_over=Decimal("0.6"),
            model_requested_stake=Money(25),
            why_now="Should be rejected: competitor is busted.",
        )

    bankruptcy_events = [
        e for e in service.event_bus.events_for_competitor("openai") if e.event_type == CompetitionEventType.BANKRUPTCY
    ]
    assert len(bankruptcy_events) == 1
