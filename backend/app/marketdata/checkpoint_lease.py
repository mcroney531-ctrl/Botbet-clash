"""Atomic claim on an eligible checkpoint cycle (seam doc §3.8).

The read-only preflight stops a scheduler paying twice IN SEQUENCE. It
cannot stop two workers racing:

    worker A: preflight -> ELIGIBLE
    worker B: preflight -> ELIGIBLE
    worker A: pays for refresh
    worker B: pays for refresh
    worker A: captures
    worker B: finds CAPTURED

Both paid, for a checkpoint only one of them could ever capture. The
preflight is deliberately read-only, so the decision has to be made atomic
somewhere else — here.

Three constraints shape the implementation:

1. **The claim commits immediately.** A transaction left open would be
   held across the provider HTTP call and the retry backoff, which is the
   one thing this whole choreography exists to avoid. `SELECT FOR UPDATE`
   is not usable for the same reason.
2. **A crashed worker must not block the checkpoint forever.** The lease
   expires; the next worker reclaims it. That is why the claim is an
   upsert guarded on expiry rather than a bare INSERT.
3. **The decision is made by the database, not by a read-then-write.**
   `INSERT ... ON CONFLICT DO UPDATE ... WHERE` is one statement, so two
   workers issuing it concurrently cannot both win.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db.session import session_scope


def default_owner() -> str:
    """Diagnostic identity only. The lease's authority comes from the
    UNIQUE constraint and the expiry, never from trusting this string."""

    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    acquired: bool
    lease_id: uuid.UUID | None
    owner: str
    expires_at: datetime | None
    held_by: str | None = None
    held_until: datetime | None = None

    @property
    def blocked_reason(self) -> str | None:
        if self.acquired:
            return None
        return (
            f"another worker holds the cycle lease (owner={self.held_by}, "
            f"expires {self.held_until})"
        )


_CLAIM = text(
    """
    INSERT INTO checkpoint_cycle_leases
        (id, game_id, checkpoint_type, owner, acquired_at, expires_at)
    VALUES
        (:id, :game_id, :checkpoint_type, :owner, :now, :expires_at)
    ON CONFLICT (game_id, checkpoint_type) DO UPDATE
        SET id = EXCLUDED.id,
            owner = EXCLUDED.owner,
            acquired_at = EXCLUDED.acquired_at,
            expires_at = EXCLUDED.expires_at
        WHERE checkpoint_cycle_leases.expires_at <= EXCLUDED.acquired_at
    RETURNING id
    """
)
"""One statement, so two concurrent workers cannot both win.

`SET id = EXCLUDED.id` is load-bearing, not tidiness. Without it a
takeover keeps the abandoned row's primary key, so the worker that overran
its lease still holds a matching `lease_id` -- and its release would
delete the NEW holder's lease on the way out. A fresh id on every claim is
what makes `release_cycle`'s scoping mean anything.


The `WHERE` on the conflict branch is what makes an UNEXPIRED lease
un-stealable: the UPDATE simply does not fire, no row is RETURNING-ed, and
the loser learns it lost without having spent anything. An expired lease
falls through the same clause and is reclaimed in the same breath, so a
crashed worker costs one lease duration rather than a whole checkpoint.
"""


def claim_cycle(
    *,
    game_id: uuid.UUID,
    checkpoint_type: str,
    now: datetime,
    duration_seconds: float,
    owner: str | None = None,
) -> LeaseClaim:
    """Try to become the one worker allowed to spend on this checkpoint.

    Commits before returning. The caller does its network work with NO
    database transaction open.
    """

    who = owner or default_owner()
    expires_at = now + timedelta(seconds=duration_seconds)

    with session_scope() as session:
        row = session.execute(
            _CLAIM,
            {
                "id": uuid.uuid4(),
                "game_id": game_id,
                "checkpoint_type": checkpoint_type,
                "owner": who,
                "now": now,
                "expires_at": expires_at,
            },
        ).first()
        if row is not None:
            return LeaseClaim(
                acquired=True, lease_id=row[0], owner=who, expires_at=expires_at
            )

        # Lost the race. Report who holds it, for diagnosis only — the
        # decision was already made by the statement above.
        held = session.execute(
            text(
                "SELECT owner, expires_at FROM checkpoint_cycle_leases "
                "WHERE game_id = :game_id AND checkpoint_type = :checkpoint_type"
            ),
            {"game_id": game_id, "checkpoint_type": checkpoint_type},
        ).first()

    return LeaseClaim(
        acquired=False,
        lease_id=None,
        owner=who,
        expires_at=None,
        held_by=held[0] if held else None,
        held_until=held[1] if held else None,
    )


def release_cycle(*, game_id: uuid.UUID, checkpoint_type: str, lease_id: uuid.UUID) -> bool:
    """Give the claim back, in its own short transaction.

    Scoped to `lease_id` so a worker that overran its lease — and whose
    claim has since been taken over by someone else — cannot delete the new
    holder's lease on its way out. Returns whether a row was removed.
    """

    with session_scope() as session:
        result = session.execute(
            text(
                "DELETE FROM checkpoint_cycle_leases "
                "WHERE id = :lease_id AND game_id = :game_id "
                "AND checkpoint_type = :checkpoint_type"
            ),
            {"lease_id": lease_id, "game_id": game_id, "checkpoint_type": checkpoint_type},
        )
        return result.rowcount > 0
