"""SeasonService — the Phase 1 stand-in for CommissionerService
(ARCHITECTURE.md §3): the one place that enforces weekly state,
one-decision-per-week, Pounce limits, stake caps, and bankruptcy, and that
emits every `CompetitionEvent`. No competitor grades its own performance —
this service is the only writer of ledger and event state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.clock import Clock
from app.domain.enums import (
    BankrollTransactionType,
    CompetitionEventType,
    CompetitorStatus,
    Side,
    SportsbookResult,
    TicketStatus,
    Uncertainty,
    Urgency,
    WagerExecutionStatus,
    WeekStatus,
)
from app.domain.errors import (
    CompetitorBusted,
    DuplicateSettlement,
    DuplicateWeeklyDecision,
    InvalidStateTransition,
    InvalidStakeIncrement,
    LineOutsideAcceptableBoundary,
    PounceLimitExceeded,
    PriceOutsideAcceptableBoundary,
    PushableLineNotAllowed,
    StakeBelowMinimum,
    StakeExceedsCap,
    TicketExpired,
    TicketNotExecutable,
)
from app.domain.events import CompetitionEventBus
from app.domain.ledger import BankrollLedger
from app.domain.lines import is_pushable_line
from app.domain.models import (
    BankrollTransaction,
    Competitor,
    CompetitionEvent,
    FakePropMarket,
    PassDecision,
    RiskPosture,
    Season,
    Settlement,
    Ticket,
    Wager,
    Week,
)
from app.domain.risk import (
    kelly_reference_stake,
    line_is_acceptable,
    price_is_acceptable,
    resolve_final_allowed_stake,
)
from app.core.money import Money


class SeasonService:
    def __init__(self, season: Season, clock: Clock, ledger: BankrollLedger, event_bus: CompetitionEventBus) -> None:
        self.season = season
        self.clock = clock
        self.ledger = ledger
        self.event_bus = event_bus
        self._weeks: dict[str, Week] = {}
        self._competitors: dict[str, Competitor] = {}
        self._tickets: dict[str, Ticket] = {}
        self._wagers: dict[str, Wager] = {}
        self._settlements_by_wager_id: dict[str, Settlement] = {}
        self._pass_decisions: dict[tuple[str, str], PassDecision] = {}

    # -- setup ---------------------------------------------------------

    def register_competitor(self, competitor: Competitor) -> Competitor:
        if competitor.id in self._competitors:
            raise InvalidStateTransition(f"competitor {competitor.id} already registered")
        self._competitors[competitor.id] = competitor
        self.ledger.record(
            BankrollTransaction(
                competitor_id=competitor.id,
                type=BankrollTransactionType.SEASON_START,
                amount=self.season.rules.starting_bankroll,
                reason="season start",
            )
        )
        return competitor

    # -- weekly lifecycle ------------------------------------------------

    def open_week(self, week: Week) -> Week:
        if week.status is not WeekStatus.PENDING:
            raise InvalidStateTransition(f"week {week.id} is not PENDING (status={week.status})")
        week.status = WeekStatus.OPENED
        week.opened_at = self.clock.now()
        self._weeks[week.id] = week
        self._publish(week.id, None, CompetitionEventType.WEEK_OPENED, {"week_number": week.week_number})
        return week

    def issue_ticket(
        self,
        *,
        week: Week,
        competitor: Competitor,
        market: FakePropMarket,
        side: Side,
        urgency: Urgency,
        risk_posture: RiskPosture,
        model_probability_over: Decimal,
        model_requested_stake: Money,
        why_now: str,
        acceptable_line_boundary: Decimal | None = None,
        worst_acceptable_price: int | None = None,
        valid_until: datetime | None = None,
    ) -> Ticket:
        self._ensure_solvent(competitor, week.id)
        if is_pushable_line(market.line):
            raise PushableLineNotAllowed(
                f"market {market.id} has a pushable line ({market.line}); "
                "RULES.md §6a requires non-pushable lines for V1 Competition wagers"
            )
        if urgency is Urgency.POUNCE and len(self._active_pounce_tickets(competitor.id, week.id)) >= self.season.rules.pounce_limit:
            raise PounceLimitExceeded(
                f"competitor {competitor.id} already has {self.season.rules.pounce_limit} active Pounce ticket(s) for week {week.id}"
            )

        available = self.ledger.available_balance(competitor.id)
        price = market.over_price if side is Side.OVER else market.under_price
        win_probability = model_probability_over if side is Side.OVER else (Decimal(1) - model_probability_over)
        kelly_stake = kelly_reference_stake(self.season.rules, available, win_probability, price)
        final_allowed = resolve_final_allowed_stake(self.season.rules, available, urgency, model_requested_stake)

        ticket = Ticket(
            competitor_id=competitor.id,
            week_id=week.id,
            market_id=market.id,
            side=side,
            urgency=urgency,
            risk_posture=risk_posture,
            kelly_reference_stake=kelly_stake,
            model_requested_stake=model_requested_stake,
            final_allowed_stake=final_allowed,
            observed_line=market.line,
            why_now=why_now,
            acceptable_line_boundary=acceptable_line_boundary,
            worst_acceptable_price=worst_acceptable_price,
            valid_until=valid_until,
            created_at=self.clock.now(),
        )
        self._tickets[ticket.id] = ticket
        event_type = CompetitionEventType.POUNCE_ISSUED if urgency is Urgency.POUNCE else CompetitionEventType.TICKET_LOCKED
        self._publish(
            week.id,
            competitor.id,
            event_type,
            {
                "ticket_id": ticket.id,
                "market_id": market.id,
                "final_allowed_stake_cents": final_allowed.cents,
                "kelly_reference_stake_cents": kelly_stake.cents,
            },
        )
        return ticket

    def record_pass(
        self,
        *,
        week: Week,
        competitor: Competitor,
        reason_for_pass: str,
        best_available_candidate: str | None = None,
        estimated_edge: Decimal | None = None,
        confidence: Decimal | None = None,
        uncertainty: Uncertainty | None = None,
    ) -> PassDecision:
        self._require_active(competitor)
        if self._has_weekly_decision(competitor.id, week.id):
            raise DuplicateWeeklyDecision(f"competitor {competitor.id} already has an official decision for week {week.id}")
        decision = PassDecision(
            competitor_id=competitor.id,
            week_id=week.id,
            reason_for_pass=reason_for_pass,
            best_available_candidate=best_available_candidate,
            estimated_edge=estimated_edge,
            confidence=confidence,
            uncertainty=uncertainty,
            created_at=self.clock.now(),
        )
        self._pass_decisions[(competitor.id, week.id)] = decision
        self._publish(week.id, competitor.id, CompetitionEventType.PASS_DECLARED, {"reason_for_pass": reason_for_pass})
        return decision

    def record_execution(
        self,
        *,
        ticket: Ticket,
        status: WagerExecutionStatus,
        sportsbook: str | None = None,
        actual_line: Decimal | None = None,
        actual_price: int | None = None,
        actual_stake: Money | None = None,
    ) -> Wager:
        """The authoritative validation gate for turning a ticket into a
        wager (constitution §75-76). A ticket can only be resolved once:
        every non-PLACED status also permanently retires it, so this can
        never be called twice for the same ticket. PLACED additionally
        enforces every hard constraint the ticket carries — none of these
        can be bypassed by whatever recorded the human's execution input.
        """

        competitor = self._competitors[ticket.competitor_id]
        self._ensure_solvent(competitor, ticket.week_id)

        if ticket.status is not TicketStatus.ISSUED:
            raise TicketNotExecutable(f"ticket {ticket.id} is not ISSUED (status={ticket.status}); already resolved")

        if status is WagerExecutionStatus.PLACED:
            if ticket.valid_until is not None and self.clock.now() > ticket.valid_until:
                raise TicketExpired(f"ticket {ticket.id} expired at {ticket.valid_until}; cannot be PLACED")
            if self._has_weekly_decision(ticket.competitor_id, ticket.week_id):
                raise DuplicateWeeklyDecision(
                    f"competitor {ticket.competitor_id} already has an official decision for week {ticket.week_id}"
                )
            if actual_stake is None or actual_line is None or actual_price is None:
                raise ValueError("actual_stake, actual_line, and actual_price are all required when status is PLACED")

            available_before = self.ledger.available_balance(ticket.competitor_id)

            if actual_stake > ticket.final_allowed_stake:
                raise StakeExceedsCap(
                    f"actual_stake {actual_stake} exceeds ticket {ticket.id}'s final_allowed_stake {ticket.final_allowed_stake}"
                )
            if actual_stake > available_before:
                raise StakeExceedsCap(f"actual_stake {actual_stake} exceeds available bankroll {available_before}")
            if actual_stake < self.season.rules.minimum_stake:
                raise StakeBelowMinimum(f"actual_stake {actual_stake} is below minimum_stake {self.season.rules.minimum_stake}")
            increment = self.season.rules.stake_increment.cents
            if increment > 0 and actual_stake.cents % increment != 0:
                raise InvalidStakeIncrement(
                    f"actual_stake {actual_stake} is not a multiple of stake_increment {self.season.rules.stake_increment}"
                )

            if ticket.acceptable_line_boundary is not None and not line_is_acceptable(
                ticket.side, ticket.acceptable_line_boundary, actual_line
            ):
                raise LineOutsideAcceptableBoundary(
                    f"actual_line {actual_line} is past ticket {ticket.id}'s acceptable boundary "
                    f"{ticket.acceptable_line_boundary} for side {ticket.side}"
                )
            if ticket.worst_acceptable_price is not None and not price_is_acceptable(
                ticket.worst_acceptable_price, actual_price
            ):
                raise PriceOutsideAcceptableBoundary(
                    f"actual_price {actual_price} is worse than ticket {ticket.id}'s worst_acceptable_price "
                    f"{ticket.worst_acceptable_price}"
                )

        wager = Wager(
            ticket_id=ticket.id,
            competitor_id=ticket.competitor_id,
            week_id=ticket.week_id,
            market_id=ticket.market_id,
            requested_stake=ticket.final_allowed_stake,
            execution_status=status,
            sportsbook=sportsbook,
            actual_line=actual_line,
            actual_price=actual_price,
            actual_stake=actual_stake,
            execution_timestamp=self.clock.now(),
        )
        self._wagers[wager.id] = wager

        if status is WagerExecutionStatus.PLACED:
            assert actual_stake is not None
            # Captured before the STAKE debit below, so this mirrors
            # `bankroll_at_decision` (constitution §106): "what bankroll was
            # this wager sized against," not the post-debit remainder.
            wager.bankroll_at_execution = available_before
            self.ledger.record(
                BankrollTransaction(
                    competitor_id=ticket.competitor_id,
                    week_id=ticket.week_id,
                    wager_id=wager.id,
                    type=BankrollTransactionType.STAKE,
                    amount=-actual_stake,
                    reason="wager placed",
                )
            )
            ticket.status = TicketStatus.EXECUTED
            self._publish(
                ticket.week_id,
                ticket.competitor_id,
                CompetitionEventType.BET_EXECUTED,
                {"wager_id": wager.id, "actual_stake_cents": actual_stake.cents},
            )
        elif status is WagerExecutionStatus.MISSED_WINDOW:
            ticket.status = TicketStatus.EXPIRED
            self._publish(ticket.week_id, ticket.competitor_id, CompetitionEventType.TICKET_EXPIRED, {"ticket_id": ticket.id})
        else:
            ticket.status = TicketStatus.SKIPPED

        return wager

    def settle_wager(self, *, wager: Wager, result: SportsbookResult, payout: Money) -> Settlement:
        if wager.execution_status is not WagerExecutionStatus.PLACED:
            raise InvalidStateTransition(f"wager {wager.id} was never PLACED; nothing to settle")
        if wager.id in self._settlements_by_wager_id:
            raise DuplicateSettlement(f"wager {wager.id} was already settled; settling it again would double-credit the ledger")

        settlement = Settlement(wager_id=wager.id, result=result, payout=payout, settled_at=self.clock.now())
        self._settlements_by_wager_id[wager.id] = settlement

        credit_type = {
            SportsbookResult.WIN: BankrollTransactionType.WIN_RETURN,
            SportsbookResult.PUSH: BankrollTransactionType.PUSH_RETURN,
            SportsbookResult.VOID: BankrollTransactionType.VOID_RETURN,
        }.get(result)
        if credit_type is not None and payout.cents > 0:
            self.ledger.record(
                BankrollTransaction(
                    competitor_id=wager.competitor_id,
                    week_id=wager.week_id,
                    wager_id=wager.id,
                    type=credit_type,
                    amount=payout,
                    reason=f"sportsbook settlement: {result.value}",
                )
            )

        outcome_event = {
            SportsbookResult.WIN: CompetitionEventType.PROP_WON,
            SportsbookResult.LOSS: CompetitionEventType.PROP_LOST,
        }.get(result, CompetitionEventType.SPORTSBOOK_SETTLED)
        self._publish(
            wager.week_id,
            wager.competitor_id,
            outcome_event,
            {"wager_id": wager.id, "result": result.value, "payout_cents": payout.cents},
        )
        self._publish(
            wager.week_id,
            wager.competitor_id,
            CompetitionEventType.BANKROLL_CHANGED,
            {"available_balance_cents": self.ledger.available_balance(wager.competitor_id).cents},
        )
        self._maybe_declare_bankruptcy(wager.competitor_id, wager.week_id)
        return settlement

    def close_week(self, week: Week) -> Week:
        if week.status is WeekStatus.CLOSED:
            raise InvalidStateTransition(f"week {week.id} is already CLOSED")
        week.status = WeekStatus.CLOSED
        week.closed_at = self.clock.now()
        self._publish(week.id, None, CompetitionEventType.WEEK_CLOSED, {"week_number": week.week_number})
        return week

    # -- queries ---------------------------------------------------------

    def tickets_for(self, competitor_id: str, week_id: str) -> tuple[Ticket, ...]:
        return tuple(t for t in self._tickets.values() if t.competitor_id == competitor_id and t.week_id == week_id)

    def wagers_for(self, competitor_id: str, week_id: str) -> tuple[Wager, ...]:
        return tuple(w for w in self._wagers.values() if w.competitor_id == competitor_id and w.week_id == week_id)

    def pass_decision_for(self, competitor_id: str, week_id: str) -> PassDecision | None:
        return self._pass_decisions.get((competitor_id, week_id))

    # -- internals ---------------------------------------------------------

    def _publish(
        self,
        week_id: str | None,
        competitor_id: str | None,
        event_type: CompetitionEventType,
        payload: dict,
    ) -> CompetitionEvent:
        event = CompetitionEvent(
            season_id=self.season.id,
            event_type=event_type,
            week_id=week_id,
            competitor_id=competitor_id,
            payload=payload,
            timestamp=self.clock.now(),
        )
        self.event_bus.publish(event)
        return event

    def _require_active(self, competitor: Competitor) -> None:
        if competitor.status is CompetitorStatus.BUSTED:
            raise CompetitorBusted(f"competitor {competitor.id} is BUSTED; real-money bankroll is frozen for the season")

    def _ensure_solvent(self, competitor: Competitor, week_id: str) -> None:
        """Safety-net re-check before any real-money action (issuing or
        executing a ticket): `_maybe_declare_bankruptcy` is normally
        triggered by `settle_wager`, but a non-settlement ledger mutation
        (e.g. a Commissioner ADJUSTMENT) can push a competitor into the
        bankrupt zone with no settlement ever happening. Re-evaluating
        here means that gets caught at the next real-money touchpoint
        instead of leaving `status` stale as ACTIVE indefinitely. Once all
        bankroll mutations flow through one ledger service (Phase 2+),
        that service can re-check on every write instead of relying on
        call sites to remember this — see ARCHITECTURE.md §3.
        """

        self._require_active(competitor)
        self._maybe_declare_bankruptcy(competitor.id, week_id)
        self._require_active(competitor)

    def _active_pounce_tickets(self, competitor_id: str, week_id: str) -> list[Ticket]:
        return [
            t
            for t in self._tickets.values()
            if t.competitor_id == competitor_id
            and t.week_id == week_id
            and t.urgency is Urgency.POUNCE
            and t.status is TicketStatus.ISSUED
        ]

    def _has_weekly_decision(self, competitor_id: str, week_id: str) -> bool:
        if (competitor_id, week_id) in self._pass_decisions:
            return True
        return any(
            w.competitor_id == competitor_id and w.week_id == week_id and w.execution_status is WagerExecutionStatus.PLACED
            for w in self._wagers.values()
        )

    def _maybe_declare_bankruptcy(self, competitor_id: str, week_id: str) -> None:
        competitor = self._competitors[competitor_id]
        if competitor.status is CompetitorStatus.BUSTED:
            return
        available = self.ledger.available_balance(competitor_id)
        # "available < minimum_stake" (constitution §79's literal wording)
        # is a special case of the real condition: no legal wager is
        # possible once even the most permissive (exceptional/Pounce) cap
        # can no longer clear the minimum stake. That's a strict
        # generalization — exceptional_cap(available) <= available always
        # — so this also catches the case where a competitor still has a
        # nonzero balance but every stake cap available to them now rounds
        # under the minimum, which record_execution would otherwise reject
        # forever without ever actually declaring them BUSTED.
        floored_cap = self.season.rules.exceptional_cap(available).floor_to_increment(self.season.rules.stake_increment)
        if floored_cap < self.season.rules.minimum_stake:
            competitor.status = CompetitorStatus.BUSTED
            self._publish(
                week_id,
                competitor_id,
                CompetitionEventType.BANKRUPTCY,
                {"available_balance_cents": available.cents},
            )
