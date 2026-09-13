"""Enumerations frozen by RULES.md. Phase 1 subset — Forecast Lab-specific
enums (checkpoint types, exclusion reasons, etc.) land in Phase 2."""

from __future__ import annotations

from enum import StrEnum


class SeasonStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"


class WeekStatus(StrEnum):
    PENDING = "PENDING"
    OPENED = "OPENED"
    DECISIONS_LOCKED = "DECISIONS_LOCKED"
    GAMES_COMPLETE = "GAMES_COMPLETE"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"


class CompetitorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BUSTED = "BUSTED"


class Side(StrEnum):
    OVER = "OVER"
    UNDER = "UNDER"


class Uncertainty(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Urgency(StrEnum):
    WATCH = "WATCH"
    LEAN = "LEAN"
    STRONG = "STRONG"
    POUNCE = "POUNCE"


class RiskPosture(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    STANDARD = "STANDARD"
    AGGRESSIVE = "AGGRESSIVE"


class TicketStatus(StrEnum):
    ISSUED = "ISSUED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"


class WagerExecutionStatus(StrEnum):
    PLACED = "PLACED"
    MARKET_MOVED = "MARKET_MOVED"
    UNAVAILABLE = "UNAVAILABLE"
    MISSED_WINDOW = "MISSED_WINDOW"
    SKIPPED = "SKIPPED"


class SportsbookResult(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    PUSH = "PUSH"
    VOID = "VOID"


class BankrollTransactionType(StrEnum):
    SEASON_START = "SEASON_START"
    STAKE = "STAKE"
    WIN_RETURN = "WIN_RETURN"
    PUSH_RETURN = "PUSH_RETURN"
    VOID_RETURN = "VOID_RETURN"
    ADJUSTMENT = "ADJUSTMENT"


class CompetitionEventType(StrEnum):
    WEEK_OPENED = "WEEK_OPENED"
    WATCHLIST_CREATED = "WATCHLIST_CREATED"
    POUNCE_ISSUED = "POUNCE_ISSUED"
    TICKET_LOCKED = "TICKET_LOCKED"
    TICKET_EXPIRED = "TICKET_EXPIRED"
    BET_EXECUTED = "BET_EXECUTED"
    PASS_DECLARED = "PASS_DECLARED"
    PROP_WON = "PROP_WON"
    PROP_LOST = "PROP_LOST"
    BANKROLL_CHANGED = "BANKROLL_CHANGED"
    BANKRUPTCY = "BANKRUPTCY"
    SPORTSBOOK_SETTLED = "SPORTSBOOK_SETTLED"
    WEEK_SETTLED = "WEEK_SETTLED"
    WEEK_CLOSED = "WEEK_CLOSED"
