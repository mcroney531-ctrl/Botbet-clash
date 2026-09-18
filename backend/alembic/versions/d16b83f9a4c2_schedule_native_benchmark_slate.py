"""Schedule-native benchmark slate: plan-owned fixture pool + provenance.

Revision ID: d16b83f9a4c2
Revises: c94a06f2e5d7
Create Date: 2026-09-18

The benchmark pool was `list[Game]`, and a `Game` exists only once the
market provider has posted the event -- so THE ODDS API's posting horizon
silently decided which fixtures a precommitted research sample could draw
from. Two unlisted Week-3 events could change a slate that nflverse
already knew all sixteen fixtures for.

`benchmark_slate_fixtures` holds the complete pool the allocator saw,
owned by its plan, with `game_id` NULL until the event is registered and
the slot is bound. Plan-owned rather than a shared table: this is a frozen
historical artifact of one commitment, and a global fixture table would be
a second source of truth that later schedule releases could rewrite
underneath an already-committed sample.

`benchmark_slots.game_id` becomes NULLABLE because a slot is now about a
planned FIXTURE, not about a database row that may not exist yet. The
column stays for pre-4A.7 rows; nothing the current core writes sets it.

The CHECK makes an official plan without provenance impossible in the
database, not merely discouraged in the service -- the same fail-closed
shape used for week corrections.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d16b83f9a4c2"
down_revision: Union[str, Sequence[str], None] = "c94a06f2e5d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLAN = "benchmark_slate_plans"
SLOT = "benchmark_slots"
FIXTURE = "benchmark_slate_fixtures"
CHECK = "official_plan_requires_provenance"


def upgrade() -> None:
    op.create_table(
        FIXTURE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("fixture_key", sa.String(), nullable=False),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column("away_team_canonical", sa.String(), nullable=False),
        sa.Column("home_team_canonical", sa.String(), nullable=False),
        sa.Column("planned_kickoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], [f"{PLAN}.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "fixture_key", name="one_fixture_per_plan"),
    )

    with op.batch_alter_table(PLAN) as batch:
        batch.add_column(sa.Column("is_official", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("rules_version", sa.String(), nullable=True))
        batch.add_column(sa.Column("schedule_provider_call_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("resolver_version", sa.String(), nullable=True))
        batch.add_column(sa.Column("fixture_key_version", sa.String(), nullable=True))
        batch.add_column(sa.Column("fixture_pool_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("fixture_pool_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("planning_input_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("earliest_opening_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            None, "provider_calls", ["schedule_provider_call_id"], ["id"],
        )

    op.create_check_constraint(
        CHECK, PLAN,
        "NOT is_official OR ("
        " rules_version IS NOT NULL"
        " AND schedule_provider_call_id IS NOT NULL"
        " AND resolver_version IS NOT NULL"
        " AND fixture_key_version IS NOT NULL"
        " AND fixture_pool_fingerprint IS NOT NULL"
        " AND planning_input_fingerprint IS NOT NULL"
        " AND fixture_pool_count IS NOT NULL"
        " AND earliest_opening_at IS NOT NULL)",
    )

    # NULL = no allocator has been reviewed and frozen for this season. Not
    # a default, and not permission to inherit ROUND_ROBIN_BY_KICKOFF_V0.
    op.add_column(
        "season_rules",
        sa.Column("benchmark_allocation_method", sa.String(), nullable=True),
    )

    op.add_column(SLOT, sa.Column("slate_fixture_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(None, SLOT, FIXTURE, ["slate_fixture_id"], ["id"])
    op.alter_column(SLOT, "game_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.alter_column(SLOT, "game_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint(
        op.f("fk_benchmark_slots_slate_fixture_id_benchmark_slate_fixtures"),
        SLOT, type_="foreignkey",
    )
    op.drop_column(SLOT, "slate_fixture_id")
    op.drop_column("season_rules", "benchmark_allocation_method")
    op.drop_constraint(CHECK, PLAN, type_="check")
    with op.batch_alter_table(PLAN) as batch:
        batch.drop_constraint(
            op.f("fk_benchmark_slate_plans_schedule_provider_call_id_provider_calls"),
            type_="foreignkey",
        )
        for column in (
            "earliest_opening_at", "planning_input_fingerprint",
            "fixture_pool_fingerprint", "fixture_pool_count",
            "fixture_key_version", "resolver_version", "schedule_provider_call_id",
            "rules_version", "is_official",
        ):
            batch.drop_column(column)
    op.drop_table(FIXTURE)
