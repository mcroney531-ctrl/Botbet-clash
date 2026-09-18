"""Planning-input fingerprint + full official-plan provenance CHECK.

Revision ID: e2c7f41a9b58
Revises: d16b83f9a4c2
Create Date: 2026-09-18

A SEPARATE revision on purpose. `d16b83f9a4c2` had already been applied to
production when these two changes were decided, and alembic never re-runs
an applied revision -- editing it in place would have left production
permanently missing the column while every local database had it, with the
version table claiming both were identical. That is the exact failure mode
a migration chain exists to prevent, so the delta gets its own revision.

`fixture_pool_fingerprint` answers "which fixtures" and deliberately
ignores kickoff, so a flexed broadcast time does not make every committed
plan compare unequal to itself. `planning_input_fingerprint` covers the
key AND the UTC-normalized kickoff, because kickoff is what the commit
deadline is computed from and what a kickoff-aware allocator would
consume. One value cannot honestly mean both "same pool" and "same input".

The CHECK is replaced rather than added to: it also now requires
`resolver_version` and `fixture_key_version`, so an official plan that
cannot name the rule that classified its fixtures or the format its keys
were spelled in is invalid in the database, not merely unreachable through
today's service.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2c7f41a9b58"
down_revision: Union[str, Sequence[str], None] = "d16b83f9a4c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLAN = "benchmark_slate_plans"
CHECK = "official_plan_requires_provenance"

OLD_CHECK = (
    "NOT is_official OR ("
    " rules_version IS NOT NULL"
    " AND schedule_provider_call_id IS NOT NULL"
    " AND fixture_pool_fingerprint IS NOT NULL"
    " AND fixture_pool_count IS NOT NULL"
    " AND earliest_opening_at IS NOT NULL)"
)
NEW_CHECK = (
    "NOT is_official OR ("
    " rules_version IS NOT NULL"
    " AND schedule_provider_call_id IS NOT NULL"
    " AND resolver_version IS NOT NULL"
    " AND fixture_key_version IS NOT NULL"
    " AND fixture_pool_fingerprint IS NOT NULL"
    " AND planning_input_fingerprint IS NOT NULL"
    " AND fixture_pool_count IS NOT NULL"
    " AND earliest_opening_at IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        PLAN,
        sa.Column("planning_input_fingerprint", sa.String(length=64), nullable=True),
    )
    op.drop_constraint(op.f(f"ck_{PLAN}_{CHECK}"), PLAN, type_="check")
    op.create_check_constraint(CHECK, PLAN, NEW_CHECK)


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{PLAN}_{CHECK}"), PLAN, type_="check")
    op.create_check_constraint(CHECK, PLAN, OLD_CHECK)
    op.drop_column(PLAN, "planning_input_fingerprint")
