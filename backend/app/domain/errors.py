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
    pass


class DuplicateWeeklyDecision(RuleViolation):
    """A competitor may only reach one official BET or PASS per week."""
