"""capture policy in season rules

Phase 4A.5. Additive and nullable: the tolerance and the refresh retry
budget become part of a season's FROZEN rules rather than an operator's
command line, because both materially change which market state may reach
an irreversible capture.

NO VALUE IS SET on any existing row. NULL means "no capture policy
frozen", which is exactly what every pre-4A.5 season ran under -- not a
zero-second tolerance and not zero attempts. Assigning 900 to a season is
a separate, deliberate provisioning decision, not a migration side effect.

Revision ID: f3a71d9b28c4
Revises: e5c1a93f2b64
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a71d9b28c4"
down_revision: Union[str, Sequence[str], None] = "e5c1a93f2b64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.add_column(
        "season_rules",
        sa.Column("max_observation_age_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "season_rules",
        sa.Column("refresh_retry_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "max_observation_age_non_negative",
        "season_rules",
        "max_observation_age_seconds IS NULL OR max_observation_age_seconds >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_constraint("max_observation_age_non_negative", "season_rules", type_="check")
    op.drop_column("season_rules", "refresh_retry_policy")
    op.drop_column("season_rules", "max_observation_age_seconds")
