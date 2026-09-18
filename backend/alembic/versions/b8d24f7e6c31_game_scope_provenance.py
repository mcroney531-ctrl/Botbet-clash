"""game scope provenance and audited correction

Phase 4A.6 closeout. Two new tables, nothing altered.

game_scope_observations answers "which exact schedule snapshot classified
this event as week N". Before this, the /events call was persisted but the
schedule fetch that actually decided Game.week_number was consumed and
discarded -- so the provenance chain stopped one step short of the field
that matters most, on a field treated as permanent identity scope.

game_scope_corrections exists because silent repair is forbidden but
audited correction is not. Leaving a known-false week in the durable
record forever is not better integrity than correcting it in the open.

Revision ID: b8d24f7e6c31
Revises: a71e5c3d94f8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8d24f7e6c31"
down_revision: Union[str, Sequence[str], None] = "a71e5c3d94f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "game_scope_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("market_provider_call_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_provider_call_id", sa.Uuid(), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("resolved_week_number", sa.Integer(), nullable=False),
        sa.Column("canonical_home", sa.String(), nullable=False),
        sa.Column("canonical_away", sa.String(), nullable=False),
        sa.Column("market_kickoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_kickoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kickoff_drift_seconds", sa.Integer(), nullable=True),
        sa.Column("resolver_version", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_game_scope_observations_game_id_games")),
        sa.ForeignKeyConstraint(
            ["market_provider_call_id"], ["provider_calls.id"],
            name=op.f("fk_game_scope_observations_market_provider_call_id_provider_calls"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_provider_call_id"], ["provider_calls.id"],
            name=op.f("fk_game_scope_observations_schedule_provider_call_id_provider_calls"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_scope_observations")),
    )
    op.create_table(
        "game_scope_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("field_corrected", sa.String(), nullable=False),
        sa.Column("old_value", sa.String(), nullable=False),
        sa.Column("new_value", sa.String(), nullable=False),
        sa.Column("schedule_provider_call_id", sa.Uuid(), nullable=True),
        sa.Column("resolver_version", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_game_scope_corrections_game_id_games")),
        sa.ForeignKeyConstraint(
            ["schedule_provider_call_id"], ["provider_calls.id"],
            name=op.f("fk_game_scope_corrections_schedule_provider_call_id_provider_calls"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_scope_corrections")),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_table("game_scope_corrections")
    op.drop_table("game_scope_observations")
