"""Bankroll ledger (RULES.md §12, constitution §78).

Current bankroll is never stored — it is always the sum of an append-only
`BankrollTransaction` log. A `STAKE` row is written as a *negative* amount
at execution time, so cash at risk falls out of the derived balance
immediately; nothing extra needs to be subtracted to get "available
bankroll" (DATABASE.md §7).
"""

from __future__ import annotations

from app.core.clock import Clock
from app.core.money import Money
from app.domain.models import BankrollTransaction


class BankrollLedger:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._transactions: list[BankrollTransaction] = []

    def record(self, transaction: BankrollTransaction) -> BankrollTransaction:
        if transaction.created_at is None:
            transaction.created_at = self._clock.now()
        self._transactions.append(transaction)
        return transaction

    def balance(self, competitor_id: str) -> Money:
        total = Money.zero()
        for txn in self._transactions:
            if txn.competitor_id == competitor_id:
                total = total + txn.amount
        return total

    # Kept as a distinct method (rather than an alias) so call sites read
    # as "the number stake caps are computed from" — see RULES.md §12.
    def available_balance(self, competitor_id: str) -> Money:
        return self.balance(competitor_id)

    def transactions_for(self, competitor_id: str) -> tuple[BankrollTransaction, ...]:
        return tuple(t for t in self._transactions if t.competitor_id == competitor_id)

    @property
    def all_transactions(self) -> tuple[BankrollTransaction, ...]:
        return tuple(self._transactions)
