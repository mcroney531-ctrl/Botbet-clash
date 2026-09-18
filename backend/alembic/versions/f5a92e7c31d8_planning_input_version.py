"""Persist WHICH serialization produced the planning-input digest.

Revision ID: f5a92e7c31d8
Revises: e2c7f41a9b58
Create Date: 2026-09-18

`planning_input_fingerprint` folds `PLANNING_INPUT_VERSION` into the digest,
which stops two formats colliding but does not tell an auditor holding the
hash which format produced it. A fingerprint you cannot attribute to a
serialization is a number, not a receipt.

A NEW revision rather than an edit to `e2c7f41a9b58`, which has already
been applied to production. Editing an applied revision leaves production
permanently behind while the version table claims otherwise -- the mistake
`e2c7f41a9b58` itself exists to correct.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a92e7c31d8"
down_revision: Union[str, Sequence[str], None] = "e2c7f41a9b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLAN = "benchmark_slate_plans"
CHECK = "official_plan_requires_provenance"

PREVIOUS = (
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
CURRENT = (
    "NOT is_official OR ("
    " rules_version IS NOT NULL"
    " AND schedule_provider_call_id IS NOT NULL"
    " AND resolver_version IS NOT NULL"
    " AND fixture_key_version IS NOT NULL"
    " AND fixture_pool_fingerprint IS NOT NULL"
    " AND planning_input_fingerprint IS NOT NULL"
    " AND planning_input_version IS NOT NULL"
    " AND fixture_pool_count IS NOT NULL"
    " AND earliest_opening_at IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(PLAN, sa.Column("planning_input_version", sa.String(), nullable=True))
    op.drop_constraint(op.f(f"ck_{PLAN}_{CHECK}"), PLAN, type_="check")
    op.create_check_constraint(CHECK, PLAN, CURRENT)


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{PLAN}_{CHECK}"), PLAN, type_="check")
    op.create_check_constraint(CHECK, PLAN, PREVIOUS)
    op.drop_column(PLAN, "planning_input_version")
