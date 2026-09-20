"""Where money may move, and where it may not. Pure — no session, no I/O.

`Week.is_real_money` was a label. Nothing read it: `issue_ticket` accepted
a rehearsal week, `record_execution(PLACED)` wrote a `STAKE` transaction
against it, `settle_wager` credited `WIN_RETURN`, and
`LedgerRepository.record` persisted whatever it was handed. A week marked
REHEARSAL could therefore move the official bankroll through the ordinary
competition path.

The fix is NOT to disable competition during a rehearsal. RULES.md §3 and
CONSTITUTION.md §6 require the rehearsal week to exercise stake
calculations, wager execution, settlement, receipts and audit
reconstruction — a rehearsal that skips the logic proves nothing about the
logic. So the same validation runs either way and the branch is at the
SIDE-EFFECT boundary only.

This module holds the policy; the services hold the choreography.
"""

from __future__ import annotations

from app.domain.enums import WagerExecutionStatus
from app.domain.week_profile import WeekProfile

# Transaction types that alter the official bankroll. An ADJUSTMENT is
# included deliberately: it mutates the same SUM as every other type, and
# "it's only an adjustment" is exactly how a rehearsal would leak into the
# competitive ledger.
BANKROLL_ALTERING_TYPES = frozenset({
    "STAKE", "WIN_RETURN", "PUSH_RETURN", "VOID_RETURN", "ADJUSTMENT",
    "SEASON_START",
})


# Carried on every rehearsal event that a consumer could otherwise read as
# an instruction to act with real money. The event TYPE is the real guard --
# an unknown type is ignored, a known one is obeyed -- and this string is
# for the human who ends up reading the payload.
REHEARSAL_TICKET_ADVISORY = "REHEARSAL / SIMULATED — DO NOT PLACE"
REHEARSAL_SETTLEMENT_ADVISORY = "REHEARSAL / SIMULATED — no bankroll effect"


class RehearsalBoundaryViolation(RuntimeError):
    """A rehearsal week was about to produce a real financial effect."""


def execution_status_allowed(
    profile: WeekProfile, status: WagerExecutionStatus
) -> bool:
    """A rehearsal may not PLACE; a competitive week may not SIMULATE.

    Neither is silently translated. The caller asked for a specific
    operation and has to learn it was the wrong one -- a service that
    quietly turned PLACED into SIMULATED would let a competitive week
    believe it had a real wager on.
    """

    if profile is WeekProfile.REHEARSAL:
        return status is not WagerExecutionStatus.PLACED
    return status is not WagerExecutionStatus.SIMULATED


def may_move_money(profile: WeekProfile) -> bool:
    return profile is WeekProfile.COMPETITIVE


def execution_coheres_with_profile(
    profile: WeekProfile, status: WagerExecutionStatus
) -> bool:
    """Whether an executed wager and its week AGREE about what happened.

    PLACED belongs to a COMPETITIVE week and SIMULATED to a REHEARSAL one,
    and the pairing is a biconditional, not a permission.

    The distinction matters at settlement. Asking only "may this week move
    money?" and answering no for a PLACED wager REINTERPRETS the wager: a
    real $2 stake that already left the bankroll settles as a simulation,
    the payout is suppressed, and the money is permanently gone with no
    record saying why. That is safe in the narrow sense that nothing is
    credited, and wrong in every other sense -- it edits economic history
    to match a contradiction instead of refusing the contradiction.

    A PLACED wager on a rehearsal week is not a rehearsal. It is corrupt
    state, and the only correct response is to stop.
    """

    if not status.executes:
        return False
    return status.moves_money is may_move_money(profile)


def alters_official_bankroll(transaction_type: str) -> bool:
    return transaction_type in BANKROLL_ALTERING_TYPES
