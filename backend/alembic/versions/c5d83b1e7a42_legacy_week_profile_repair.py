"""Normalize four pre-profile smoke Weeks to the REHEARSAL profile.

Revision ID: c5d83b1e7a42
Revises: a3f81c6b57e9
Create Date: 2026-09-19

WHY THESE ROWS EXIST. Before the three-flag week profile existed, the only
way to say "this week is not for real money" was `is_real_money=False`,
and `counts_toward_standings` / `counts_toward_awards` kept their column
default of True. Four Phase-3 live-smoke harness seasons were created that
way and each carries a week 1 in the resulting shape:

    is_real_money=False, counts_toward_standings=True, counts_toward_awards=True

which is not a rehearsal and not a competitive week -- it is one of the six
combinations nobody approved. Migration b7c249e0f3a1 makes that shape
unrepresentable, and correctly refused to install itself while these rows
were present.

WHY IT IS SAFE TO NORMALIZE THEM. A production diagnostic scoped to these
four exact ids found, for every one: 0 tickets, 0 wagers, 0 pass
decisions, 0 settlements, 0 week-scoped bankroll transactions, 0 net
cents, 0 benchmark slate plans, and exactly one WEEK_OPENED event. There
is no standings, awards, wager or bankroll history for the two defaulted
flags to have influenced, so bringing them into agreement with the
`is_real_money=False` these rows already assert reinterprets nothing.

Two of them do hold research forecasts (1 and 3) and each holds one Game.
Those are legitimate smoke/research artifacts and are deliberately LEFT
ALONE -- this migration touches two boolean columns on four rows and
nothing else.

None of the four belongs to the durable BotBet Clash 2026 research season
27c34e5b-a6bb-4c05-b0ca-29aba4690808, which is verified here rather than
assumed.

The operator reviewed the diagnostic and explicitly approved normalizing
these four exact ids to REHEARSAL. A blanket
`UPDATE weeks SET ... WHERE is_real_money = false` was considered and
rejected: this project makes provenance first-class, and repairing the
very bug that motivated the new invariant should not be an anonymous
mutation. This migration IS the correction record.

ZERO OR FOUR, NEVER IN BETWEEN. A database that never held these smoke
seasons -- a fresh developer database, CI, the test suite, which builds
its schema by running this chain -- has none of these ids and nothing to
repair, so the migration is a no-op there. Production has all four.
Anything in between means a database in a state nobody has described, and
that aborts rather than guessing which half to trust.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d83b1e7a42"
down_revision: Union[str, Sequence[str], None] = "a3f81c6b57e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The four approved ids. Written out in full, deliberately: a query that
# derived them from their current flags would silently widen to whatever
# else happened to match at deploy time.
APPROVED_WEEK_IDS = (
    "e3e831a4-5001-40ac-8a5d-1aaa21564626",  # Live Provider Smoke (three models)
    "5a51ac4e-49cd-4496-abc2-3716a716914a",  # Phase 3 Live Smoke cbf0be64...
    "37ed5a76-aa38-458d-9365-d54666142181",  # Phase 3 Live Smoke 5283e4a4...
    "771fdca8-1fd5-4503-8aaa-25bb1ab2b61c",  # Phase 3 Live Smoke 29816b5b...
)

DURABLE_SEASON_ID = "27c34e5b-a6bb-4c05-b0ca-29aba4690808"

# The shape every approved row must be in BEFORE the repair. Not "roughly
# a rehearsal" -- exactly the defaulted-flags shape the diagnostic found.
EXPECTED_BEFORE = (False, True, True)
REPAIRED_TO = (False, False, False)

_PREFLIGHT = sa.text("""
    SELECT
        w.id,
        w.season_id,
        w.is_real_money,
        w.counts_toward_standings,
        w.counts_toward_awards,
        (SELECT count(*) FROM tickets t WHERE t.week_id = w.id) AS tickets,
        (SELECT count(*) FROM wagers g WHERE g.week_id = w.id) AS wagers,
        (SELECT count(*) FROM pass_decisions p WHERE p.week_id = w.id) AS passes,
        (SELECT count(*) FROM settlements s
            JOIN wagers g2 ON g2.id = s.wager_id
            WHERE g2.week_id = w.id) AS settlements,
        (SELECT count(*) FROM bankroll_transactions b WHERE b.week_id = w.id) AS money_rows,
        (SELECT count(*) FROM benchmark_slate_plans pl WHERE pl.week_id = w.id) AS plans
    FROM weeks w
    WHERE w.id = ANY(:ids)
""")

# Artifact counts that must all be zero. Games and ForecastObservations are
# NOT in this list on purpose: they are the smoke runs' research output,
# they are unaffected by the two flags being corrected, and requiring them
# to be absent would refuse a repair for the wrong reason.
MUST_BE_ZERO = ("tickets", "wagers", "passes", "settlements", "money_rows", "plans")


def _abort(reason: str) -> None:
    raise RuntimeError(
        "legacy week-profile repair REFUSED, nothing written.\n"
        f"  {reason}\n"
        "  This migration repairs four specific reviewed rows and declines "
        "to act on a database that does not look the way that review found "
        "it. Re-run the scoped diagnostic and bring the discrepancy back "
        "for an explicit decision."
    )


def _approved_rows(bind):
    """Rows for the approved ids, verified. Empty tuple means nothing to do."""

    rows = bind.execute(_PREFLIGHT, {"ids": list(APPROVED_WEEK_IDS)}).fetchall()
    if not rows:
        return ()
    if len(rows) != len(APPROVED_WEEK_IDS):
        found = ", ".join(str(r.id) for r in rows)
        _abort(
            f"expected either 0 or {len(APPROVED_WEEK_IDS)} of the approved "
            f"week rows to be present; found {len(rows)}: {found}"
        )
    return tuple(rows)


def upgrade() -> None:
    bind = op.get_bind()
    rows = _approved_rows(bind)
    if not rows:
        print(
            "legacy week-profile repair: none of the four approved week rows "
            "is present; nothing to repair."
        )
        return

    for row in rows:
        flags = (
            row.is_real_money,
            row.counts_toward_standings,
            row.counts_toward_awards,
        )
        if flags != EXPECTED_BEFORE:
            _abort(
                f"week {row.id} is {flags}, not the reviewed pre-repair shape "
                f"{EXPECTED_BEFORE}. It has already been changed by something "
                "else."
            )
        if str(row.season_id) == DURABLE_SEASON_ID:
            _abort(
                f"week {row.id} belongs to the durable research season "
                f"{DURABLE_SEASON_ID}. The approved rows are smoke harnesses; "
                "this one is not, and the season's weeks are not repaired here."
            )
        for column in MUST_BE_ZERO:
            count = getattr(row, column)
            if count:
                _abort(
                    f"week {row.id} now has {count} {column}. The approval "
                    "rested on this week having no competition, settlement or "
                    "money history; it has some now."
                )

    result = bind.execute(
        sa.text("""
            UPDATE weeks
            SET counts_toward_standings = false,
                counts_toward_awards = false
            WHERE id = ANY(:ids)
              AND is_real_money = false
              AND counts_toward_standings = true
              AND counts_toward_awards = true
        """),
        {"ids": list(APPROVED_WEEK_IDS)},
    )
    # The WHERE clause repeats the preconditions so the write cannot touch a
    # row that changed between the preflight and here, and the count is
    # asserted so a partial write aborts the transaction rather than
    # committing half a repair.
    if result.rowcount != len(APPROVED_WEEK_IDS):
        _abort(
            f"the repair updated {result.rowcount} rows, expected "
            f"{len(APPROVED_WEEK_IDS)}"
        )
    print(
        f"legacy week-profile repair: {result.rowcount} smoke weeks normalized "
        "to REHEARSAL (is_real_money unchanged; games, forecasts and events "
        "untouched)."
    )


def downgrade() -> None:
    """Put the four rows back into their historical pre-fix shape.

    This RECREATES A NONSTANDARD PROFILE. False/True/True is not a valid
    modern week profile -- it is the defaulted-flags bug this migration
    exists to correct -- and it is restored here only so the revision is
    genuinely reversible.

    It follows that b7c249e0f3a1, which installs the constraint forbidding
    that shape, must be downgraded FIRST. Running this while that
    constraint is installed will fail on the CHECK, which is the correct
    outcome rather than something to work around.
    """

    bind = op.get_bind()
    result = bind.execute(
        sa.text("""
            UPDATE weeks
            SET counts_toward_standings = true,
                counts_toward_awards = true
            WHERE id = ANY(:ids)
              AND is_real_money = false
              AND counts_toward_standings = false
              AND counts_toward_awards = false
        """),
        {"ids": list(APPROVED_WEEK_IDS)},
    )
    print(
        f"legacy week-profile repair reverted on {result.rowcount} row(s) "
        "(restores the historical nonstandard shape)."
    )
