"""Take the week-profile backstops down, on purpose, for one block.

Migration `b7c249e0f3a1` makes a NONSTANDARD week unrepresentable and
freezes the three profile columns against UPDATE. That is the point of it
-- and it means the APPLICATION-level guards against those states can no
longer be reached through any normal path.

An unreachable guard still has to be tested. It is what remains if a
future schema change drops the constraint, if a database is restored from
a dump taken before this revision, or if someone edits the table directly.
A layer nobody exercises without the layer above it is a layer nobody has
exercised.

So tests that need corrupt state build it explicitly here, and the fact
that they MUST is itself an assertion: if `DROP CONSTRAINT` or
`DISABLE TRIGGER` ever stops being necessary, the backstop is gone.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from sqlalchemy import text

from app.db.session import session_scope

PROFILE_CHECK = "ck_weeks_reviewed_profile"
FREEZE_TRIGGER = "trg_refuse_week_profile_change"

_CHECK_BODY = (
    "(is_real_money AND counts_toward_standings AND counts_toward_awards)"
    " OR (NOT is_real_money AND NOT counts_toward_standings"
    " AND NOT counts_toward_awards)"
)


@contextmanager
def backstops_down():
    """Drop the profile CHECK and disable the freeze trigger, then restore.

    Restored in a `finally`, because these are schema-level changes that
    the per-test TRUNCATE does not undo -- leaking one would silently
    disarm every later test in the session.

    The re-add is NOT VALID: the corrupt row the caller just built is
    still present and a validating re-add would refuse it. A NOT VALID
    CHECK is still enforced on every subsequent INSERT and UPDATE; it only
    skips the one-time scan of rows already there, and the next test's
    TRUNCATE removes them.
    """

    with session_scope() as session:
        session.execute(text(f"ALTER TABLE weeks DROP CONSTRAINT {PROFILE_CHECK}"))
        session.execute(text(f"ALTER TABLE weeks DISABLE TRIGGER {FREEZE_TRIGGER}"))
    try:
        yield
    finally:
        with session_scope() as session:
            session.execute(text(f"ALTER TABLE weeks ENABLE TRIGGER {FREEZE_TRIGGER}"))
            session.execute(text(
                f"ALTER TABLE weeks ADD CONSTRAINT {PROFILE_CHECK} "
                f"CHECK ({_CHECK_BODY}) NOT VALID"
            ))


def force_week(
    season_id, week_number: int, *, is_real_money: bool,
    counts_toward_standings: bool, counts_toward_awards: bool,
    status: str = "PENDING",
) -> uuid.UUID:
    """Persist a week the schema would normally reject."""

    from app.db.models.season import Week as WeekRow

    with backstops_down():
        with session_scope() as session:
            week = WeekRow(
                season_id=season_id, week_number=week_number, status=status,
                is_real_money=is_real_money,
                counts_toward_standings=counts_toward_standings,
                counts_toward_awards=counts_toward_awards,
            )
            session.add(week)
            session.flush()
            return week.id


def corrupt_profile(
    week_id, *, is_real_money: bool, counts_toward_standings: bool,
    counts_toward_awards: bool,
) -> None:
    """Change an existing week's frozen profile."""

    from app.db.models.season import Week as WeekRow

    if isinstance(week_id, str):
        week_id = uuid.UUID(week_id)
    with backstops_down():
        with session_scope() as session:
            week = session.get(WeekRow, week_id)
            week.is_real_money = is_real_money
            week.counts_toward_standings = counts_toward_standings
            week.counts_toward_awards = counts_toward_awards
