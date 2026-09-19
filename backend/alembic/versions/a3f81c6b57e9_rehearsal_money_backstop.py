"""A rehearsal week cannot mutate the official bankroll. Enforced in the DB.

Revision ID: a3f81c6b57e9
Revises: f5a92e7c31d8
Create Date: 2026-09-19

`Week.is_real_money` was a label nothing read. `record_execution(PLACED)`
wrote a STAKE against a rehearsal week, `settle_wager` credited
WIN_RETURN, and `LedgerRepository.record` persisted whatever it was handed.
The service layer now refuses all three -- but the claim we actually want
is stronger:

    a rehearsal week cannot move official bankroll EVEN IF a future
    service bypasses the Commissioner entirely.

A CHECK constraint cannot express it: the deciding fact lives on `weeks`,
and a CHECK may not reference another table. So the invariant is a
trigger, which is the conventional Postgres tool for exactly this shape.

Two triggers, one idea:

  * `bankroll_transactions` -- refuse any bankroll-altering row whose
    `week_id` resolves to a week with `is_real_money = false`. Season-start
    funding carries `week_id IS NULL` and is untouched.
  * `wagers` -- refuse `execution_status = 'PLACED'` on such a week. A
    rehearsal records SIMULATED.

ADJUSTMENT is included deliberately. It mutates the same SUM as every
other type, and "it's only an adjustment" is precisely how a rehearsal
would leak into the competitive ledger.

Nothing is backfilled or rewritten: production currently holds no
rehearsal-week money rows, and the triggers validate only new writes.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a3f81c6b57e9"
down_revision: Union[str, Sequence[str], None] = "f5a92e7c31d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEDGER_FN = "refuse_rehearsal_bankroll_movement"
WAGER_FN = "refuse_rehearsal_placed_wager"
LEDGER_TRIGGER = "trg_refuse_rehearsal_bankroll_movement"
WAGER_TRIGGER = "trg_refuse_rehearsal_placed_wager"

# Kept in lockstep with app.domain.rehearsal.BANKROLL_ALTERING_TYPES.
ALTERING = "('STAKE','WIN_RETURN','PUSH_RETURN','VOID_RETURN','ADJUSTMENT','SEASON_START')"


def upgrade() -> None:
    op.execute(f"""
    CREATE OR REPLACE FUNCTION {LEDGER_FN}() RETURNS trigger AS $$
    DECLARE
        real_money boolean;
    BEGIN
        IF NEW.week_id IS NULL THEN
            RETURN NEW;
        END IF;
        IF NEW.type NOT IN {ALTERING} THEN
            RETURN NEW;
        END IF;
        SELECT w.is_real_money INTO real_money FROM weeks w WHERE w.id = NEW.week_id;
        IF real_money IS NULL THEN
            RAISE EXCEPTION
                'bankroll transaction references week % which does not exist',
                NEW.week_id;
        END IF;
        IF NOT real_money THEN
            RAISE EXCEPTION
                'refusing % of % cents against rehearsal week %: official bankroll only moves on a real-money week',
                NEW.type, NEW.amount_cents, NEW.week_id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(f"""
    CREATE TRIGGER {LEDGER_TRIGGER}
    BEFORE INSERT OR UPDATE ON bankroll_transactions
    FOR EACH ROW EXECUTE FUNCTION {LEDGER_FN}();
    """)

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
    op.execute(f"""
    CREATE TRIGGER {WAGER_TRIGGER}
    BEFORE INSERT OR UPDATE ON wagers
    FOR EACH ROW EXECUTE FUNCTION {WAGER_FN}();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {WAGER_TRIGGER} ON wagers;")
    op.execute(f"DROP FUNCTION IF EXISTS {WAGER_FN}();")
    op.execute(f"DROP TRIGGER IF EXISTS {LEDGER_TRIGGER} ON bankroll_transactions;")
    op.execute(f"DROP FUNCTION IF EXISTS {LEDGER_FN}();")
