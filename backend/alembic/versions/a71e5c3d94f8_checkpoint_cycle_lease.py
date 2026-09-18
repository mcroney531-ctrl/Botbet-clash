"""checkpoint cycle lease

Phase 4A.5 closeout. A new table, nothing altered.

The read-only preflight stops a scheduler paying twice IN SEQUENCE. It
cannot stop two workers racing: both observe ELIGIBLE before either
spends, and both buy a refresh for a checkpoint only one can capture.
This table makes the eligible-cycle claim atomic.

`expires_at` rather than a plain flag: a worker that dies mid-cycle must
not block the checkpoint forever. The claim is an upsert guarded on
expiry, so an abandoned lease is reclaimed by the next worker.

Revision ID: a71e5c3d94f8
Revises: f3a71d9b28c4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a71e5c3d94f8"
down_revision: Union[str, Sequence[str], None] = "f3a71d9b28c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "checkpoint_cycle_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_type", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_checkpoint_cycle_leases_game_id_games")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkpoint_cycle_leases")),
        # The UNIQUE constraint is what ON CONFLICT targets. Without it the
        # claim statement has nothing to conflict on and two workers both win.
        sa.UniqueConstraint("game_id", "checkpoint_type", name="one_lease_per_game_checkpoint"),
        sa.CheckConstraint("expires_at > acquired_at", name="lease_expires_after_acquisition"),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_table("checkpoint_cycle_leases")
