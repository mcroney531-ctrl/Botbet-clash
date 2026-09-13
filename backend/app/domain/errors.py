"""Domain-level exceptions. Raised by the Commissioner-owned services, never
swallowed silently — see ARCHITECTURE.md §10 (failure handling)."""

from __future__ import annotations


class DomainError(Exception):
    """Base for all rule-violation errors raised by the domain layer."""


class RuleViolation(DomainError):
    """Raised when a request would violate a frozen SeasonRules constraint."""


class CompetitorBusted(DomainError):
    """Raised when an action is attempted for a real-money-eliminated competitor."""


class InvalidStateTransition(DomainError):
    """Raised when a week/ticket/wager transition is attempted out of order."""


class PounceLimitExceeded(RuleViolation):
    pass


class StakeExceedsCap(RuleViolation):
    """actual_stake exceeds the ticket's final_allowed_stake or current available bankroll."""


class StakeBelowMinimum(RuleViolation):
    pass


class InvalidStakeIncrement(RuleViolation):
    pass


class PushableLineNotAllowed(RuleViolation):
    """RULES.md §6a: V1 official Competition wagers must use a non-pushable line."""


class TicketNotExecutable(InvalidStateTransition):
    """record_execution was called on a ticket that isn't ISSUED (already
    resolved by an earlier call)."""


class TicketExpired(RuleViolation):
    """record_execution(status=PLACED) was attempted after ticket.valid_until."""


class LineOutsideAcceptableBoundary(RuleViolation):
    pass


class PriceOutsideAcceptableBoundary(RuleViolation):
    pass


class DuplicateWeeklyDecision(RuleViolation):
    """A competitor may only reach one official BET or PASS per week."""


class DuplicateSettlement(InvalidStateTransition):
    """A wager may only be settled once — settling it again would credit
    (or attempt to credit) the ledger twice for the same outcome."""
