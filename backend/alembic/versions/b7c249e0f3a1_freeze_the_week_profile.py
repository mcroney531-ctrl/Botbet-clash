"""The week profile is frozen at preparation. Make that true in the database.

Revision ID: b7c249e0f3a1
Revises: a3f81c6b57e9
Create Date: 2026-09-19

Phase 4A.8 made the SERVICES refuse to move money on a rehearsal week and
added triggers so a bypass could not either. Review found that the guards
were built around a possibility the preparation contract denies: that an
operator can flip a week's three profile booleans afterwards. The tests
even exercised it -- a COMPETITIVE week with a real $2 stake debited, the
week flipped to REHEARSAL, and the settlement then suppressing the payout.
Nothing was credited, which is safe, and the competitor was permanently
down the stake with the record calling it a rehearsal, which is not.

That is impossible state, not supported state. Three changes:

1. A CHECK constraint so `weeks` can only hold one of the two reviewed
   profiles. NONSTANDARD becomes unrepresentable rather than merely
   refused by application code.

2. A BEFORE UPDATE trigger refusing any change to the three profile
   columns. Every other column -- status, opened_at, closed_at,
   research_locked_at -- stays freely updatable, because opening and
   closing a week is ordinary lifecycle. No code path gets to turn a
   REHEARSAL into a COMPETITIVE week in place; a new profile would need a
   schema and rules change, which is the point.

3. The `wagers` trigger from a3f81c6b57e9 becomes SYMMETRIC. It refused
   PLACED on a non-real-money week but permitted SIMULATED on a real-money
   one, so a raw writer could still put a simulated wager on a competitive
   week. With the CHECK in place, `is_real_money` alone distinguishes the
   two persisted profiles, so the function can require the biconditional.
   It is replaced with CREATE OR REPLACE FUNCTION; a3f81c6b57e9 is an
   applied production migration and is NOT edited.

FAIL CLOSED ON EXISTING DATA. If any week row is already nonstandard the
upgrade ABORTS and names the rows. It does not normalize them. A silent
repair inside a migration would decide, unreviewed, whether weeks that are
already part of the season record counted toward standings -- and would do
it at deploy time, where nobody is reading.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c249e0f3a1"
down_revision: Union[str, Sequence[str], None] = "a3f81c6b57e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The metadata naming convention prefixes "ck_<table>_", so this short
# name becomes "ck_weeks_reviewed_profile" on the table. Alembic applies
# the same convention on DROP, so the short name is what BOTH ends use --
# passing the rendered name to drop_constraint double-prefixes it into
# "ck_weeks_ck_weeks_reviewed_profile", which the round-trip test caught.
PROFILE_CHECK = "reviewed_profile"
FREEZE_FN = "refuse_week_profile_change"
FREEZE_TRIGGER = "trg_refuse_week_profile_change"
WAGER_FN = "refuse_rehearsal_placed_wager"

# The two reviewed profiles, as SQL. CONSTITUTION.md §6 and RULES.md §3:
# a rehearsal week carries no real wagers, no standings and no awards; a
# competitive week is the complement.
VALID_PROFILE = """
    (is_real_money AND counts_toward_standings AND counts_toward_awards)
    OR
    (NOT is_real_money AND NOT counts_toward_standings AND NOT counts_toward_awards)
"""


def upgrade() -> None:
    bind = op.get_bind()

    offenders = bind.execute(sa.text(f"""
        SELECT id, week_number, is_real_money, counts_toward_standings,
               counts_toward_awards
        FROM weeks
        WHERE NOT ({VALID_PROFILE})
        ORDER BY week_number
    """)).fetchall()
    if offenders:
        listing = "\n".join(
            f"    week {r.week_number} ({r.id}): is_real_money={r.is_real_money}, "
            f"counts_toward_standings={r.counts_toward_standings}, "
            f"counts_toward_awards={r.counts_toward_awards}"
            for r in offenders
        )
        raise RuntimeError(
            f"{len(offenders)} week row(s) match no reviewed profile:\n{listing}\n"
            "Refusing to install the profile constraint, and refusing to "
            "normalize these rows here. Whether a half-rehearsal week counted "
            "toward the season is a decision for an operator who can see the "
            "week's artifacts, not for a migration running at deploy time. "
            "Repair them explicitly, then re-run."
        )

    op.create_check_constraint(PROFILE_CHECK, "weeks", sa.text(VALID_PROFILE))

    op.execute(f"""
    CREATE OR REPLACE FUNCTION {FREEZE_FN}() RETURNS trigger AS $$
    BEGIN
        IF NEW.is_real_money IS DISTINCT FROM OLD.is_real_money
           OR NEW.counts_toward_standings IS DISTINCT FROM OLD.counts_toward_standings
           OR NEW.counts_toward_awards IS DISTINCT FROM OLD.counts_toward_awards
        THEN
            RAISE EXCEPTION
                'week % profile is frozen at preparation: refusing to change (%, %, %) to (%, %, %)',
                NEW.id,
                OLD.is_real_money, OLD.counts_toward_standings, OLD.counts_toward_awards,
                NEW.is_real_money, NEW.counts_toward_standings, NEW.counts_toward_awards;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(f"""
    CREATE TRIGGER {FREEZE_TRIGGER}
    BEFORE UPDATE ON weeks
    FOR EACH ROW EXECUTE FUNCTION {FREEZE_FN}();
    """)

    # Symmetric now, in both directions. The trigger itself is unchanged
    # and stays attached; only the function body is replaced.
    op.execute(f"""
    CREATE OR REPLACE FUNCTION {WAGER_FN}() RETURNS trigger AS $$
    DECLARE
        real_money boolean;
    BEGIN
        IF NEW.execution_status NOT IN ('PLACED', 'SIMULATED') THEN
            RETURN NEW;
        END IF;
        SELECT w.is_real_money INTO real_money FROM weeks w WHERE w.id = NEW.week_id;
        IF real_money IS NULL THEN
            RAISE EXCEPTION 'wager references week % which does not exist', NEW.week_id;
        END IF;
        IF NEW.execution_status = 'PLACED' AND NOT real_money THEN
            RAISE EXCEPTION
                'refusing a PLACED wager on rehearsal week %: a rehearsal records SIMULATED',
                NEW.week_id;
        END IF;
        IF NEW.execution_status = 'SIMULATED' AND real_money THEN
            RAISE EXCEPTION
                'refusing a SIMULATED wager on competitive week %: a competitive week records PLACED',
                NEW.week_id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    # Back to the one-directional a3f81c6b57e9 body, verbatim, so a
    # downgrade lands on exactly the shape that revision installed.
    op.execute(f"""
    CREATE OR REPLACE FUNCTION {WAGER_FN}() RETURNS trigger AS $$
    DECLARE
        real_money boolean;
    BEGIN
        IF NEW.execution_status <> 'PLACED' THEN
            RETURN NEW;
        END IF;
        SELECT w.is_real_money INTO real_money FROM weeks w WHERE w.id = NEW.week_id;
        IF real_money IS NULL THEN
            RAISE EXCEPTION 'wager references week % which does not exist', NEW.week_id;
        END IF;
        IF NOT real_money THEN
            RAISE EXCEPTION
                'refusing a PLACED wager on rehearsal week %: a rehearsal records SIMULATED',
                NEW.week_id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(f"DROP TRIGGER IF EXISTS {FREEZE_TRIGGER} ON weeks;")
    op.execute(f"DROP FUNCTION IF EXISTS {FREEZE_FN}();")
    op.drop_constraint(PROFILE_CHECK, "weeks", type_="check")
