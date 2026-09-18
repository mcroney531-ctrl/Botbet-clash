"""SeasonCommissioner — the Phase 2A persistent equivalent of
`app/domain/season_service.SeasonService`.

Same rules, same domain exceptions, same pure risk/validation math
(`app/domain/risk.py`, `app/domain/lines.py`) — but every public method
opens its own `session_scope()` and commits or rolls back atomically, and
the object itself holds no cached state beyond `season_id`. That last
point is deliberate: a fresh `SeasonCommissioner(season_id)` constructed
in a brand new process must behave identically to one that's been running
the whole time, because nothing is remembered anywhere but the database.

Per ARCHITECTURE.md §6/§7: each method is one unit of work (one commit),
and every state change that should be reconstructable also writes a
`CompetitionEvent` row in that same transaction — "persist first" needs
no separate dispatch step yet because there is no async subscriber in
Phase 2; that seam is where one gets added later without changing this
class's transaction shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from app.core.clock import Clock, SystemClock
from app.core.money import Money
from app.db.mappers.competition import (
    bankroll_transaction_to_domain,
    competition_event_to_domain,
    pass_decision_to_domain,
    season_rules_to_domain,
    settlement_to_domain,
    ticket_to_domain,
    wager_to_domain,
)
from app.db.models.competition import PassDecision as PassDecisionRow
from app.db.models.competition import Ticket as TicketRow
from app.db.models.competition import Wager as WagerRow
from app.db.models.events import CompetitionEvent as CompetitionEventRow
from app.db.models.season import SeasonCompetitor as SeasonCompetitorRow
from app.db.models.season import Week as WeekRow
from app.db.models.settlement import BankrollTransaction as BankrollTransactionRow
from app.db.models.settlement import Settlement as SettlementRow
from app.db.repositories.competitor_repository import CompetitorRepository
from app.db.repositories.competition_repository import CompetitionRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.ledger_repository import LedgerRepository
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.enums import (
    CompetitionEventType,
    CompetitorStatus,
    RiskPosture,
    Side,
    SportsbookResult,
    TicketStatus,
    Uncertainty,
    Urgency,
    WagerExecutionStatus,
)
from app.domain.errors import (
    CompetitorBusted,
    CrossSeasonReference,
    DuplicateSettlement,
    DuplicateWeeklyDecision,
    InvalidStakeIncrement,
    InvalidStateTransition,
    LineOutsideAcceptableBoundary,
    MarketNotInWeek,
    NoLegalStakeAvailable,
    PounceLimitExceeded,
    PriceOutsideAcceptableBoundary,
    PushableLineNotAllowed,
    StakeBelowMinimum,
    StakeExceedsCap,
    TicketExpired,
    TicketNotExecutable,
    WeekNotOpen,
)
from app.marketdata.provenance import SYNTHETIC_SOURCE
from app.domain.lines import is_pushable_line
from app.domain.models import SeasonRules as DomainSeasonRules
from app.domain.risk import (
    kelly_reference_stake,
    line_is_acceptable,
    price_is_acceptable,
    resolve_final_allowed_stake,
)


class SeasonCommissioner:
    def __init__(self, season_id: str, clock: Clock | None = None) -> None:
        self.season_id: uuid.UUID = uuid.UUID(str(season_id))
        self.clock = clock or SystemClock()

    # -- season / rules setup ---------------------------------------------

    @classmethod
    def create_season(
        cls,
        *,
        name: str,
        year: int,
        rules: DomainSeasonRules,
        clock: Clock | None = None,
        market_data_provider: str = SYNTHETIC_SOURCE,
        roster_data_provider: str = SYNTHETIC_SOURCE,
    ) -> "SeasonCommissioner":
        """Create a season and its first rules row.

        The two provider pins default to SYNTHETIC so that a season created
        without saying otherwise can never accidentally claim to be backed
        by real market or roster data. A real research season passes them
        explicitly.
        """

        clock = clock or SystemClock()
        with session_scope() as session:
            season_row = SeasonRepository(session).create_season(name=name, year=year)
            SeasonRepository(session).create_season_rules(
                season_id=season_row.id,
                rules=rules,
                effective_from=clock.now(),
                market_data_provider=market_data_provider,
                roster_data_provider=roster_data_provider,
            )
            season_id = season_row.id
        return cls(season_id=str(season_id), clock=clock)

    def rules(self) -> DomainSeasonRules:
        with session_scope() as session:
            return season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))

    # -- competitors -------------------------------------------------------

    def register_competitor(
        self,
        *,
        competitor_id: str,
        provider: str,
        display_name: str,
        model_identifier: str,
        model_version: str,
    ) -> str:
        with session_scope() as session:
            competitor_repo = CompetitorRepository(session)
            competitor_repo.ensure_identity(competitor_id=competitor_id, provider=provider, display_name=display_name)
            season_competitor = competitor_repo.register_season_competitor(
                season_id=self.season_id,
                competitor_id=competitor_id,
                model_identifier=model_identifier,
                model_version=model_version,
                frozen_at=self.clock.now(),
            )
            rules = season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))
            LedgerRepository(session).record(
                BankrollTransactionRow(
                    season_competitor_id=season_competitor.id,
                    type="SEASON_START",
                    amount_cents=rules.starting_bankroll.cents,
                    reason="season start",
                    created_at=self.clock.now(),
                )
            )
            return str(season_competitor.id)

    def available_balance(self, season_competitor_id: str) -> Money:
        with session_scope() as session:
            return LedgerRepository(session).available_balance(uuid.UUID(season_competitor_id))

    def competitor_status(self, season_competitor_id: str) -> CompetitorStatus:
        with session_scope() as session:
            row = CompetitorRepository(session).get(uuid.UUID(season_competitor_id))
            return CompetitorStatus(row.status)

    # -- weeks -------------------------------------------------------------

    def prepare_week(self, *, week_number: int, is_real_money: bool) -> str:
        """Create the week PENDING without opening the competition.

        A benchmark slate must be committed before the first OPENING
        window, and `BenchmarkSlatePlan.week_id` needs a Week row to point
        at. Before this existed, the only way to get that row was
        `open_week`, which declares the competition week open and emits
        WEEK_OPENED -- so precommitting the research sample required
        starting the competition first. Those are two different events and
        this makes them two different calls.

        Emits NO event, moves no bankroll, touches no competitor and calls
        no provider. Idempotent for an identical PENDING week.
        """

        with session_scope() as session:
            week = SeasonRepository(session).prepare_week(
                season_id=self.season_id, week_number=week_number,
                is_real_money=is_real_money,
            )
            return str(week.id)

    def open_week(self, *, week_number: int, is_real_money: bool) -> str:
        with session_scope() as session:
            week = SeasonRepository(session).open_week(
                season_id=self.season_id, week_number=week_number, is_real_money=is_real_money, opened_at=self.clock.now()
            )
            self._publish(session, week_id=week.id, season_competitor_id=None, event_type=CompetitionEventType.WEEK_OPENED,
                           payload={"week_number": week_number})
            return str(week.id)

    def close_week(self, week_id: str) -> None:
        with session_scope() as session:
            week_uuid = uuid.UUID(week_id)
            week = self._require_week_in_season(session, week_uuid)

            # NOTE - this checks GAMES_COMPLETE, not research-settlement-
            # locked, and those are not the same thing. `Game.status ==
            # FINAL` means the football game ended; it says nothing about
            # whether research settlement (RULES.md §82's T+72h lock,
            # tracked by `research_settlements`/`weeks.research_locked_at`)
            # has actually happened for that game's markets. The eventual
            # weekly orchestrator (ARCHITECTURE.md §4's per-game state
            # machine) should gate closing on a proper
            # GAMES_COMPLETE -> (research settlements locked) -> SETTLED ->
            # CLOSED sequence; this is a deliberately narrower stand-in
            # ("all games finished playing") until that orchestrator
            # exists. A week with no games attached (e.g. a Week 0 harness
            # fixture) has nothing to block on.
            unfinished = [
                g.external_ref
                for g in MarketRepository(session).games_for_week(self.season_id, week.week_number)
                if g.status != "FINAL"
            ]
            if unfinished:
                raise InvalidStateTransition(
                    f"week {week_id} cannot be closed: game(s) {unfinished} have not finished playing (status != FINAL)"
                )

            week = SeasonRepository(session).close_week(week_uuid, closed_at=self.clock.now())
            self._publish(session, week_id=week.id, season_competitor_id=None, event_type=CompetitionEventType.WEEK_CLOSED,
                           payload={"week_number": week.week_number})

    # -- tickets -------------------------------------------------------------

    def issue_ticket(
        self,
        *,
        week_id: str,
        season_competitor_id: str,
        market_id: str,
        side: Side,
        urgency: Urgency,
        risk_posture: RiskPosture,
        model_probability_over: Decimal,
        price_for_side: int,
        observed_line: Decimal,
        model_requested_stake: Money,
        why_now: str,
        acceptable_line_boundary: Decimal | None = None,
        worst_acceptable_price: int | None = None,
        valid_until: datetime | None = None,
    ) -> str:
        with session_scope() as session:
            week_uuid, competitor_uuid, market_uuid = uuid.UUID(week_id), uuid.UUID(season_competitor_id), uuid.UUID(market_id)

            week = self._require_week_in_season(session, week_uuid)
            self._require_competitor_in_season(session, competitor_uuid)
            self._require_market_in_week(session, market_uuid, week)
            self._require_week_open(week)
            self._lock_competitor_week(session, competitor_uuid, week_uuid)

            self._ensure_solvent(session, competitor_uuid, week_uuid)

            comp_repo = CompetitionRepository(session)
            if self._has_weekly_decision(comp_repo, competitor_uuid, week_uuid):
                # BET_EXECUTED and PASS_LOCKED are terminal (ARCHITECTURE.md
                # §4 Axis 2) - a new pending ticket must never coexist with
                # one. Without this, a ticket issued after the decision is
                # already locked in would just be rejected later at
                # record_execution time, but it would still sit around as
                # an ISSUED ticket contradicting the derived decision state.
                raise DuplicateWeeklyDecision(
                    f"competitor {season_competitor_id} already has an official decision for week {week_id}"
                )

            if is_pushable_line(observed_line):
                raise PushableLineNotAllowed(
                    f"market {market_id} has a pushable line ({observed_line}); "
                    "RULES.md §6a requires non-pushable lines for V1 Competition wagers"
                )

            if urgency is Urgency.POUNCE:
                rules = season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))
                active = comp_repo.active_pounce_tickets(competitor_uuid, week_uuid, self.clock.now())
                if len(active) >= rules.pounce_limit:
                    raise PounceLimitExceeded(
                        f"competitor {season_competitor_id} already has {rules.pounce_limit} active Pounce ticket(s) for week {week_id}"
                    )

            rules = season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))
            available = LedgerRepository(session).available_balance(competitor_uuid)
            win_probability = model_probability_over if side is Side.OVER else (Decimal(1) - model_probability_over)
            kelly_stake = kelly_reference_stake(rules, available, win_probability, price_for_side)
            final_allowed = resolve_final_allowed_stake(rules, available, urgency, model_requested_stake)
            if final_allowed < rules.minimum_stake:
                raise NoLegalStakeAvailable(
                    f"competitor {season_competitor_id}'s final_allowed_stake for this ticket floors to {final_allowed}, "
                    f"below minimum_stake {rules.minimum_stake} — no execution of this ticket could ever be legal "
                    f"(requested {model_requested_stake}, {urgency.value} cap on {available} available bankroll)."
                )

            ticket_row = TicketRow(
                season_competitor_id=competitor_uuid,
                week_id=week_uuid,
                market_id=market_uuid,
                urgency=urgency.value,
                side=side.value,
                risk_posture=risk_posture.value,
                kelly_reference_stake_cents=kelly_stake.cents,
                model_requested_stake_cents=model_requested_stake.cents,
                final_allowed_stake_cents=final_allowed.cents,
                observed_line=observed_line,
                observed_price=price_for_side,
                acceptable_line_boundary=acceptable_line_boundary,
                worst_acceptable_price=worst_acceptable_price,
                why_now=why_now,
                valid_until=valid_until,
                status="ISSUED",
                created_at=self.clock.now(),
            )
            comp_repo.add_ticket(ticket_row)

            event_type = CompetitionEventType.POUNCE_ISSUED if urgency is Urgency.POUNCE else CompetitionEventType.TICKET_LOCKED
            self._publish(
                session,
                week_id=week_uuid,
                season_competitor_id=competitor_uuid,
                event_type=event_type,
                payload={
                    "ticket_id": str(ticket_row.id),
                    "market_id": market_id,
                    "final_allowed_stake_cents": final_allowed.cents,
                    "kelly_reference_stake_cents": kelly_stake.cents,
                },
            )
            return str(ticket_row.id)

    def get_ticket(self, ticket_id: str):
        with session_scope() as session:
            return ticket_to_domain(CompetitionRepository(session).get_ticket(uuid.UUID(ticket_id)))

    def record_pass(
        self,
        *,
        week_id: str,
        season_competitor_id: str,
        reason_for_pass: str,
        best_available_candidate_market_id: str | None = None,
        estimated_edge: Decimal | None = None,
        confidence: Decimal | None = None,
        uncertainty: Uncertainty | None = None,
    ) -> str:
        with session_scope() as session:
            week_uuid, competitor_uuid = uuid.UUID(week_id), uuid.UUID(season_competitor_id)

            week = self._require_week_in_season(session, week_uuid)
            self._require_competitor_in_season(session, competitor_uuid)
            if best_available_candidate_market_id:
                self._require_market_in_week(session, uuid.UUID(best_available_candidate_market_id), week)
            self._require_week_open(week)
            self._lock_competitor_week(session, competitor_uuid, week_uuid)

            competitor = CompetitorRepository(session).get(competitor_uuid)
            if competitor.status == "BUSTED":
                raise CompetitorBusted(f"competitor {season_competitor_id} is BUSTED; real-money bankroll is frozen for the season")

            comp_repo = CompetitionRepository(session)
            if self._has_weekly_decision(comp_repo, competitor_uuid, week_uuid):
                raise DuplicateWeeklyDecision(
                    f"competitor {season_competitor_id} already has an official decision for week {week_id}"
                )
            row = PassDecisionRow(
                season_competitor_id=competitor_uuid,
                week_id=week_uuid,
                best_available_candidate_market_id=(
                    uuid.UUID(best_available_candidate_market_id) if best_available_candidate_market_id else None
                ),
                estimated_edge=estimated_edge,
                confidence=confidence,
                uncertainty=(uncertainty.value if uncertainty is not None else None),
                reason_for_pass=reason_for_pass,
                created_at=self.clock.now(),
            )
            comp_repo.add_pass_decision(row)
            self._publish(
                session,
                week_id=week_uuid,
                season_competitor_id=competitor_uuid,
                event_type=CompetitionEventType.PASS_DECLARED,
                payload={"reason_for_pass": reason_for_pass},
            )
            return str(row.id)

    # -- execution -------------------------------------------------------

    def record_execution(
        self,
        *,
        ticket_id: str,
        status: WagerExecutionStatus,
        sportsbook: str | None = None,
        actual_line: Decimal | None = None,
        actual_price: int | None = None,
        actual_stake: Money | None = None,
    ) -> str:
        """Mirrors `domain.season_service.SeasonService.record_execution`
        exactly, including the fix where solvency is only re-checked for
        `PLACED` — see that method's docstring for why."""

        with session_scope() as session:
            comp_repo = CompetitionRepository(session)
            ticket_uuid = uuid.UUID(ticket_id)
            ticket = comp_repo.get_ticket(ticket_uuid)

            # A ticket_id from another season's Commissioner must be
            # rejected before anything below applies this season's rules
            # to it or writes an event mislabeled with this season_id.
            self._require_week_in_season(session, ticket.week_id)

            if ticket.status != "ISSUED":
                raise TicketNotExecutable(f"ticket {ticket_id} is not ISSUED (status={ticket.status}); already resolved")

            competitor_uuid = ticket.season_competitor_id
            rules = season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))
            ledger_repo = LedgerRepository(session)

            if status is WagerExecutionStatus.PLACED:
                self._lock_competitor_week(session, competitor_uuid, ticket.week_id)
                week = SeasonRepository(session).get_week(ticket.week_id)
                self._require_week_open(week)
                self._ensure_solvent(session, competitor_uuid, ticket.week_id)
                if ticket.valid_until is not None and self.clock.now() > ticket.valid_until:
                    raise TicketExpired(f"ticket {ticket_id} expired at {ticket.valid_until}; cannot be PLACED")
                if self._has_weekly_decision(comp_repo, competitor_uuid, ticket.week_id):
                    raise DuplicateWeeklyDecision(
                        f"competitor {competitor_uuid} already has an official decision for week {ticket.week_id}"
                    )
                if actual_stake is None or actual_line is None or actual_price is None:
                    raise ValueError("actual_stake, actual_line, and actual_price are all required when status is PLACED")

                available_before = ledger_repo.available_balance(competitor_uuid)
                final_allowed = Money(ticket.final_allowed_stake_cents)

                if actual_stake > final_allowed:
                    raise StakeExceedsCap(f"actual_stake {actual_stake} exceeds ticket {ticket_id}'s final_allowed_stake {final_allowed}")
                if actual_stake > available_before:
                    raise StakeExceedsCap(f"actual_stake {actual_stake} exceeds available bankroll {available_before}")
                if actual_stake < rules.minimum_stake:
                    raise StakeBelowMinimum(f"actual_stake {actual_stake} is below minimum_stake {rules.minimum_stake}")
                increment = rules.stake_increment.cents
                if increment > 0 and actual_stake.cents % increment != 0:
                    raise InvalidStakeIncrement(f"actual_stake {actual_stake} is not a multiple of stake_increment {rules.stake_increment}")

                if ticket.acceptable_line_boundary is not None and not line_is_acceptable(
                    Side(ticket.side), ticket.acceptable_line_boundary, actual_line
                ):
                    raise LineOutsideAcceptableBoundary(
                        f"actual_line {actual_line} is past ticket {ticket_id}'s acceptable boundary "
                        f"{ticket.acceptable_line_boundary} for side {ticket.side}"
                    )
                if ticket.worst_acceptable_price is not None and not price_is_acceptable(ticket.worst_acceptable_price, actual_price):
                    raise PriceOutsideAcceptableBoundary(
                        f"actual_price {actual_price} is worse than ticket {ticket_id}'s worst_acceptable_price "
                        f"{ticket.worst_acceptable_price}"
                    )

            wager_row = WagerRow(
                ticket_id=ticket.id,
                season_competitor_id=competitor_uuid,
                week_id=ticket.week_id,
                market_id=ticket.market_id,
                requested_stake_cents=ticket.final_allowed_stake_cents,
                execution_status=status.value,
                sportsbook=sportsbook,
                actual_line=actual_line,
                actual_price=actual_price,
                actual_stake_cents=(actual_stake.cents if actual_stake is not None else None),
                execution_timestamp=self.clock.now(),
            )
            comp_repo.add_wager(wager_row)

            if status is WagerExecutionStatus.PLACED:
                assert actual_stake is not None
                wager_row.bankroll_at_execution_cents = available_before.cents
                ledger_repo.record(
                    BankrollTransactionRow(
                        season_competitor_id=competitor_uuid,
                        week_id=ticket.week_id,
                        wager_id=wager_row.id,
                        type="STAKE",
                        amount_cents=-actual_stake.cents,
                        reason="wager placed",
                        created_at=self.clock.now(),
                    )
                )
                ticket.status = "EXECUTED"
                self._publish(
                    session,
                    week_id=ticket.week_id,
                    season_competitor_id=competitor_uuid,
                    event_type=CompetitionEventType.BET_EXECUTED,
                    payload={"wager_id": str(wager_row.id), "actual_stake_cents": actual_stake.cents},
                )
            elif status is WagerExecutionStatus.MISSED_WINDOW:
                ticket.status = "EXPIRED"
                self._publish(
                    session,
                    week_id=ticket.week_id,
                    season_competitor_id=competitor_uuid,
                    event_type=CompetitionEventType.TICKET_EXPIRED,
                    payload={"ticket_id": ticket_id},
                )
            else:
                ticket.status = "SKIPPED"

            return str(wager_row.id)

    def get_wager(self, wager_id: str):
        with session_scope() as session:
            return wager_to_domain(CompetitionRepository(session).get_wager(uuid.UUID(wager_id)))

    # -- settlement -------------------------------------------------------

    def settle_wager(self, *, wager_id: str, result: SportsbookResult, payout: Money) -> str:
        with session_scope() as session:
            comp_repo = CompetitionRepository(session)
            wager_uuid = uuid.UUID(wager_id)
            wager = comp_repo.get_wager(wager_uuid)
            self._require_competitor_in_season(session, wager.season_competitor_id)

            if wager.execution_status != "PLACED":
                raise InvalidStateTransition(f"wager {wager_id} was never PLACED; nothing to settle")
            # Defense in depth: the DB UNIQUE(wager_id) constraint on
            # settlements is the hard backstop; this check exists so the
            # failure is DuplicateSettlement, not a raw IntegrityError.
            if comp_repo.get_settlement_by_wager(wager_uuid) is not None:
                raise DuplicateSettlement(f"wager {wager_id} was already settled; settling it again would double-credit the ledger")

            settlement_row = SettlementRow(
                wager_id=wager_uuid,
                sportsbook_result=result.value,
                sportsbook_payout_cents=payout.cents,
                sportsbook_settled_at=self.clock.now(),
            )
            comp_repo.add_settlement(settlement_row)

            credit_type = {
                SportsbookResult.WIN: "WIN_RETURN",
                SportsbookResult.PUSH: "PUSH_RETURN",
                SportsbookResult.VOID: "VOID_RETURN",
            }.get(result)
            if credit_type is not None and payout.cents > 0:
                LedgerRepository(session).record(
                    BankrollTransactionRow(
                        season_competitor_id=wager.season_competitor_id,
                        week_id=wager.week_id,
                        wager_id=wager.id,
                        type=credit_type,
                        amount_cents=payout.cents,
                        reason=f"sportsbook settlement: {result.value}",
                        created_at=self.clock.now(),
                    )
                )

            outcome_event = {
                SportsbookResult.WIN: CompetitionEventType.PROP_WON,
                SportsbookResult.LOSS: CompetitionEventType.PROP_LOST,
            }.get(result, CompetitionEventType.SPORTSBOOK_SETTLED)
            self._publish(
                session,
                week_id=wager.week_id,
                season_competitor_id=wager.season_competitor_id,
                event_type=outcome_event,
                payload={"wager_id": wager_id, "result": result.value, "payout_cents": payout.cents},
            )
            available_after = LedgerRepository(session).available_balance(wager.season_competitor_id)
            self._publish(
                session,
                week_id=wager.week_id,
                season_competitor_id=wager.season_competitor_id,
                event_type=CompetitionEventType.BANKROLL_CHANGED,
                payload={"available_balance_cents": available_after.cents},
            )
            self._maybe_declare_bankruptcy(session, wager.season_competitor_id, wager.week_id)
            return str(settlement_row.id)

    # -- queries -----------------------------------------------------------

    def transactions_for(self, season_competitor_id: str):
        with session_scope() as session:
            rows = LedgerRepository(session).transactions_for(uuid.UUID(season_competitor_id))
            return [bankroll_transaction_to_domain(r) for r in rows]

    def events_for_week(self, week_id: str):
        with session_scope() as session:
            rows = EventRepository(session).list_for_week(uuid.UUID(week_id))
            return [competition_event_to_domain(r) for r in rows]

    def events_for_competitor(self, season_competitor_id: str):
        with session_scope() as session:
            rows = EventRepository(session).list_for_competitor(uuid.UUID(season_competitor_id))
            return [competition_event_to_domain(r) for r in rows]

    def get_settlement(self, wager_id: str):
        with session_scope() as session:
            row = CompetitionRepository(session).get_settlement_by_wager(uuid.UUID(wager_id))
            return settlement_to_domain(row) if row is not None else None

    def get_pass_decision(self, season_competitor_id: str, week_id: str):
        with session_scope() as session:
            has_pass = CompetitionRepository(session).has_pass_decision(uuid.UUID(season_competitor_id), uuid.UUID(week_id))
            return has_pass

    # -- internals ---------------------------------------------------------

    def _publish(self, session, *, week_id, season_competitor_id, event_type: CompetitionEventType, payload: dict) -> None:
        EventRepository(session).add(
            CompetitionEventRow(
                timestamp=self.clock.now(),
                season_id=self.season_id,
                week_id=week_id,
                season_competitor_id=season_competitor_id,
                event_type=event_type.value,
                payload=payload,
            )
        )

    def _has_weekly_decision(self, comp_repo: CompetitionRepository, competitor_uuid: uuid.UUID, week_uuid: uuid.UUID) -> bool:
        return comp_repo.has_pass_decision(competitor_uuid, week_uuid) or comp_repo.has_placed_wager(competitor_uuid, week_uuid)

    def _require_week_in_season(self, session, week_uuid: uuid.UUID) -> WeekRow:
        """Every consequential operation is scoped to `self.season_id` —
        a week/competitor/market id belonging to a different season must
        be rejected before this Commissioner's rules are ever applied to
        it or an event gets written under the wrong season_id."""

        week = SeasonRepository(session).get_week(week_uuid)
        if week.season_id != self.season_id:
            raise CrossSeasonReference(f"week {week_uuid} belongs to season {week.season_id}, not this Commissioner's season {self.season_id}")
        return week

    def _require_competitor_in_season(self, session, competitor_uuid: uuid.UUID) -> SeasonCompetitorRow:
        competitor = CompetitorRepository(session).get(competitor_uuid)
        if competitor.season_id != self.season_id:
            raise CrossSeasonReference(
                f"season_competitor {competitor_uuid} belongs to season {competitor.season_id}, not this Commissioner's season {self.season_id}"
            )
        return competitor

    def _require_market_in_week(self, session, market_uuid: uuid.UUID, week: WeekRow) -> None:
        """Season membership alone isn't enough: a market's game must
        also belong to the *same week_number* as `week`, or a Week 1
        ticket could reference a Week 12 prop as long as both happen to
        share a season (RULES.md's season-scoping fix didn't cover this —
        it's a same-season, cross-week variant of the same bug class)."""

        market_repo = MarketRepository(session)
        market = market_repo.get_prop_market(market_uuid)
        game = market_repo.get_game(market.game_id)
        if game.season_id != self.season_id:
            raise CrossSeasonReference(
                f"market {market_uuid} belongs to season {game.season_id} (via game {game.id}), not this Commissioner's season {self.season_id}"
            )
        if game.week_number != week.week_number:
            raise MarketNotInWeek(
                f"market {market_uuid} belongs to week_number {game.week_number} (via game {game.id}), "
                f"not week {week.id}'s week_number {week.week_number}"
            )

    def _require_week_open(self, week: WeekRow) -> None:
        if week.status != "OPENED":
            raise WeekNotOpen(f"week {week.id} is not OPENED (status={week.status})")

    def _lock_competitor_week(self, session, competitor_uuid: uuid.UUID, week_uuid: uuid.UUID) -> None:
        """Serialize every consequential action for one (competitor, week)
        pair within a single Postgres transaction, so two concurrent
        requests can never both observe 'no official decision yet' (or
        'zero active Pounces') and both proceed. `pg_advisory_xact_lock`
        auto-releases at commit/rollback, matching this class's one-
        transaction-per-method shape exactly — no separate unlock call
        needed. A dedicated `weekly_decisions` table with
        `UNIQUE(season_competitor_id, week_id)` would be the cleaner
        long-term primitive; this is the V1 stopgap.
        """

        key = f"{competitor_uuid}:{week_uuid}"
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})

    def _ensure_solvent(self, session, competitor_uuid: uuid.UUID, week_id: uuid.UUID | None = None) -> None:
        competitor_repo = CompetitorRepository(session)
        competitor = competitor_repo.get(competitor_uuid)
        if competitor.status == "BUSTED":
            raise CompetitorBusted(f"competitor {competitor_uuid} is BUSTED; real-money bankroll is frozen for the season")
        self._maybe_declare_bankruptcy(session, competitor_uuid, week_id)
        competitor = competitor_repo.get(competitor_uuid)
        if competitor.status == "BUSTED":
            raise CompetitorBusted(f"competitor {competitor_uuid} is BUSTED; real-money bankroll is frozen for the season")

    def _maybe_declare_bankruptcy(self, session, competitor_uuid: uuid.UUID, week_id) -> None:
        competitor_repo = CompetitorRepository(session)
        competitor = competitor_repo.get(competitor_uuid)
        if competitor.status == "BUSTED":
            return
        rules = season_rules_to_domain(SeasonRepository(session).get_active_rules(self.season_id))
        available = LedgerRepository(session).available_balance(competitor_uuid)
        floored_cap = rules.exceptional_cap(available).floor_to_increment(rules.stake_increment)
        if floored_cap < rules.minimum_stake:
            competitor_repo.set_status(competitor_uuid, "BUSTED")
            self._publish(
                session,
                week_id=week_id,
                season_competitor_id=competitor_uuid,
                event_type=CompetitionEventType.BANKRUPTCY,
                payload={"available_balance_cents": available.cents},
            )
