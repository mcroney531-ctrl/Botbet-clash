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
    """A REAL wager was executed at a sportsbook. Debits the bankroll."""

    SIMULATED = "SIMULATED"
    """A rehearsal execution: every validator the real path runs, none of
    the money. Deliberately a distinct value rather than PLACED-with-a-flag,
    because `execution_status == "PLACED"` is already the condition
    downstream bankroll and settlement logic keys on -- reusing it would
    mean every one of those call sites had to remember the flag, and one
    that forgot would move real money on a rehearsal."""

    MARKET_MOVED = "MARKET_MOVED"
    UNAVAILABLE = "UNAVAILABLE"
    MISSED_WINDOW = "MISSED_WINDOW"
    SKIPPED = "SKIPPED"

    @property
    def executes(self) -> bool:
        """Whether this status means the wager was actually taken -- really
        or in rehearsal. Both run the SAME validation; they differ only in
        side effects."""

        return self in {WagerExecutionStatus.PLACED, WagerExecutionStatus.SIMULATED}

    @property
    def moves_money(self) -> bool:
        return self is WagerExecutionStatus.PLACED


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

    # Rehearsal counterparts. DISTINCT event types rather than a flag on the
    # competitive ones: a consumer that has never heard of rehearsal will
    # ignore an unknown type, whereas it would happily act on a BET_EXECUTED
    # whose payload carried a mode field it does not read. (Every payload
    # carries `week_mode` as well -- see SeasonCommissioner._publish -- but
    # that is the second line of defence, not the first.)
    #
    # The line is drawn at ACTIONABILITY. These four types each assert
    # something a downstream consumer could act on with real money: a locked
    # ticket and a Pounce both read as "place this bet", an execution reads
    # as "a wager exists at a book", and a settlement reads as "collect".
    # PASS_DECLARED and the week/bankroll lifecycle events assert no such
    # thing -- a pass is the ABSENCE of a wager, and there is nothing to
    # misread as an instruction -- so those keep one type and are
    # distinguished by `week_mode` alone.
    SIMULATED_TICKET_LOCKED = "SIMULATED_TICKET_LOCKED"
    SIMULATED_POUNCE_ISSUED = "SIMULATED_POUNCE_ISSUED"
    SIMULATED_BET_EXECUTED = "SIMULATED_BET_EXECUTED"
    SIMULATED_SETTLED = "SIMULATED_SETTLED"
    PASS_DECLARED = "PASS_DECLARED"
    PROP_WON = "PROP_WON"
    PROP_LOST = "PROP_LOST"
    BANKROLL_CHANGED = "BANKROLL_CHANGED"
    BANKRUPTCY = "BANKRUPTCY"
    SPORTSBOOK_SETTLED = "SPORTSBOOK_SETTLED"
    WEEK_SETTLED = "WEEK_SETTLED"
    WEEK_CLOSED = "WEEK_CLOSED"


class StatFamily(StrEnum):
    """The five prop families this competition forecasts.

    These values ARE the strings stored in `PropMarket.stat_type` and
    listed in `SeasonRules.supported_prop_types` — they are the project's
    own vocabulary, not any vendor's. Market-data adapters translate a
    provider's market keys into these and nothing above the adapter ever
    sees a vendor spelling.
    """

    PASSING_YARDS = "passing_yards"
    PASSING_TOUCHDOWNS = "passing_touchdowns"
    RUSHING_YARDS = "rushing_yards"
    RECEPTIONS = "receptions"
    RECEIVING_YARDS = "receiving_yards"
