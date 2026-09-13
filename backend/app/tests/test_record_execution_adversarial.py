"""record_execution is the authoritative validation gate for turning a
ticket into a real wager (constitution §75-76). These are the adversarial
cases a reviewer specifically asked for: none of these constraints may be
bypassable by whatever recorded the human's execution input.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import Money
from app.domain.enums import (
    BankrollTransactionType,
    CompetitorStatus,
    RiskPosture,
    Side,
    SportsbookResult,
    TicketStatus,
    Urgency,
    WagerExecutionStatus,
)
from app.domain.errors import (
    CompetitorBusted,
    DuplicateSettlement,
    InvalidStakeIncrement,
    LineOutsideAcceptableBoundary,
    PriceOutsideAcceptableBoundary,
    PushableLineNotAllowed,
    StakeBelowMinimum,
    StakeExceedsCap,
    TicketExpired,
    TicketNotExecutable,
)
from app.domain.models import BankrollTransaction, FakePropMarket
from app.domain.season_service import SeasonService


def market(line=Decimal("52.5"), over_price=-115, under_price=-105, market_id="mkt-1") -> FakePropMarket:
    return FakePropMarket(id=market_id, description="test market", line=line, over_price=over_price, under_price=under_price)


def issue(service: SeasonService, week, **overrides):
    defaults = dict(
        week=week,
        competitor=service._competitors["openai"],
        market=market(),
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.60"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="test",
    )
    defaults.update(overrides)
    return service.issue_ticket(**defaults)


def test_actual_stake_cannot_exceed_final_allowed_stake(service: SeasonService, week1):
    ticket = issue(service, week1)  # final_allowed_stake = $2.00
    with pytest.raises(StakeExceedsCap):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.25"),
        )


def test_actual_stake_cannot_exceed_bankroll_even_if_under_ticket_cap(service: SeasonService, week1):
    # Ticket is issued while $15 is available, capping at $2.00. Bankroll
    # then drops (e.g. an ADJUSTMENT) below that $2.00 before execution —
    # the ticket's own cap is stale and must not be trusted on its own.
    ticket = issue(service, week1)
    service.ledger.record(
        BankrollTransaction(
            competitor_id="openai",
            type=BankrollTransactionType.ADJUSTMENT,
            amount=Money.from_dollars_str("-14.00"),
            reason="test: simulate bankroll drop between issuance and execution",
        )
    )
    assert service.ledger.available_balance("openai") == Money.from_dollars_str("1.00")
    with pytest.raises(StakeExceedsCap):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_actual_stake_below_minimum_rejected(service: SeasonService, week1):
    ticket = issue(service, week1, model_requested_stake=Money.from_dollars_str("0.25"))
    with pytest.raises(StakeBelowMinimum):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money(3),  # $0.03, well under the $0.25 minimum
        )


def test_actual_stake_must_be_a_multiple_of_increment(service: SeasonService, week1):
    # final_allowed_stake is itself always increment-aligned (resolve_final_allowed_stake
    # floors it), so to exercise this check independently the requested
    # actual_stake just needs to be <= the cap but off-increment — e.g. the
    # human mis-recorded the executed amount.
    ticket = issue(service, week1, model_requested_stake=Money.from_dollars_str("3.00"))
    assert ticket.final_allowed_stake == Money.from_dollars_str("3.00")
    with pytest.raises(InvalidStakeIncrement):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money(210),  # $2.10 - under cap, but not a multiple of the $0.25 increment
        )


def test_final_allowed_stake_is_always_increment_aligned(service: SeasonService, week1):
    # Regression: resolve_final_allowed_stake used to floor the cap and
    # then take min(requested, floored_cap), which let an off-increment
    # request (e.g. $2.10 under a $3.00 cap) pass through unfloored -
    # producing a final_allowed_stake record_execution would then reject
    # outright. Flooring must happen last, after the min.
    ticket = issue(service, week1, model_requested_stake=Money.from_dollars_str("2.10"))
    assert ticket.final_allowed_stake == Money.from_dollars_str("2.00")
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=ticket.final_allowed_stake,
    )
    assert wager.actual_stake == Money.from_dollars_str("2.00")


def test_ticket_cannot_be_executed_twice(service: SeasonService, week1):
    ticket = issue(service, week1)
    service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert ticket.status is TicketStatus.EXECUTED
    with pytest.raises(TicketNotExecutable):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_skipped_ticket_cannot_later_be_placed(service: SeasonService, week1):
    ticket = issue(service, week1)
    service.record_execution(ticket=ticket, status=WagerExecutionStatus.SKIPPED)
    assert ticket.status is TicketStatus.SKIPPED
    with pytest.raises(TicketNotExecutable):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_expired_ticket_cannot_be_placed(service: SeasonService, week1, clock):
    ticket = issue(service, week1, valid_until=clock.now() + timedelta(hours=1))
    clock.advance(hours=2)
    with pytest.raises(TicketExpired):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_expired_ticket_can_still_be_recorded_missed_window(service: SeasonService, week1, clock):
    ticket = issue(service, week1, valid_until=clock.now() + timedelta(hours=1))
    clock.advance(hours=2)
    wager = service.record_execution(ticket=ticket, status=WagerExecutionStatus.MISSED_WINDOW)
    assert wager.execution_status is WagerExecutionStatus.MISSED_WINDOW
    assert ticket.status is TicketStatus.EXPIRED


def test_line_moved_past_boundary_for_over_side_rejected(service: SeasonService, week1):
    ticket = issue(service, week1, acceptable_line_boundary=Decimal("53.5"))
    with pytest.raises(LineOutsideAcceptableBoundary):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("54.5"),  # moved past the OVER boundary (higher is worse)
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_line_moved_within_boundary_for_over_side_accepted(service: SeasonService, week1):
    ticket = issue(service, week1, acceptable_line_boundary=Decimal("54.5"))
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("53.5"),  # moved, but still within the OVER boundary
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert wager.actual_line == Decimal("53.5")


def test_line_moved_past_boundary_for_under_side_rejected(service: SeasonService, week1):
    ticket = issue(service, week1, side=Side.UNDER, acceptable_line_boundary=Decimal("50.5"))
    with pytest.raises(LineOutsideAcceptableBoundary):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("49.5"),  # moved past the UNDER boundary (lower is worse)
            actual_price=-105,
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_price_worse_than_boundary_rejected(service: SeasonService, week1):
    ticket = issue(service, week1, worst_acceptable_price=-120)
    with pytest.raises(PriceOutsideAcceptableBoundary):
        service.record_execution(
            ticket=ticket,
            status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"),
            actual_price=-125,  # worse than -120
            actual_stake=Money.from_dollars_str("2.00"),
        )


def test_price_better_than_boundary_accepted(service: SeasonService, week1):
    ticket = issue(service, week1, worst_acceptable_price=-120)
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=-115,  # better than -120
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert wager.actual_price == -115


def test_price_boundary_is_sign_aware(service: SeasonService, week1):
    # Worst acceptable is -105; an actual price of +100 is a better price
    # for the bettor even though 100 as a bare number reads "smaller
    # magnitude" than 105 — this only passes if the comparison goes
    # through decimal odds rather than raw magnitude.
    ticket = issue(service, week1, worst_acceptable_price=-105)
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=100,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert wager.actual_price == 100


def test_bankroll_at_execution_is_captured_before_the_stake_debit(service: SeasonService, week1):
    ticket = issue(service, week1)  # $15.00 available at issuance
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    assert wager.bankroll_at_execution == Money.from_dollars_str("15.00")
    assert service.ledger.available_balance("openai") == Money.from_dollars_str("13.00")


def test_pushable_line_rejected_at_issuance(service: SeasonService, week1):
    with pytest.raises(PushableLineNotAllowed):
        issue(service, week1, market=market(line=Decimal("75")))


def test_adjustment_into_dead_zone_is_caught_before_next_ticket(service: SeasonService, week1):
    # No settlement happens here at all - a Commissioner ADJUSTMENT alone
    # drops available bankroll to $0.50, where even the 30% exceptional
    # cap ($0.15) can't clear the $0.25 minimum stake. Nothing marks the
    # competitor BUSTED until the next real-money touchpoint re-checks.
    competitor = service._competitors["openai"]
    service.ledger.record(
        BankrollTransaction(
            competitor_id="openai",
            type=BankrollTransactionType.ADJUSTMENT,
            amount=Money.from_dollars_str("-14.50"),
            reason="test: simulate a non-settlement ledger event that strands the competitor",
        )
    )
    assert competitor.status is CompetitorStatus.ACTIVE  # stale - nothing has re-evaluated it yet
    with pytest.raises(CompetitorBusted):
        issue(service, week1)
    assert competitor.status is CompetitorStatus.BUSTED


def test_wager_cannot_be_settled_twice(service: SeasonService, week1):
    ticket = issue(service, week1)
    wager = service.record_execution(
        ticket=ticket,
        status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    service.settle_wager(wager=wager, result=SportsbookResult.WIN, payout=Money.from_dollars_str("3.74"))
    balance_after_first_settlement = service.ledger.available_balance("openai")
    assert balance_after_first_settlement == Money.from_dollars_str("16.74")

    with pytest.raises(DuplicateSettlement):
        service.settle_wager(wager=wager, result=SportsbookResult.WIN, payout=Money.from_dollars_str("3.74"))

    # The rejected re-settlement must not have credited the ledger again.
    assert service.ledger.available_balance("openai") == balance_after_first_settlement
