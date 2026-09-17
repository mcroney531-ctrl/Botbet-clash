"""snapshot observation freshness

Phase 4A.3. Purely additive: four columns on market_snapshots recording
how the snapshot decided which observations to consume.

No backfill and no data migration. Existing rows get
max_observation_age_seconds = NULL, which is the honest value -- they were
built before a freshness gate existed, so no threshold was in force. It is
NOT the same as 0, and a snapshot must never be made to claim a rule that
did not produce it.

selected_quotes is likewise left NULL on existing rows rather than
reconstructed. The selection COULD be re-derived from prop_quotes today,
but re-derivation is not a safe substitute in general: a later historical
backfill can legitimately insert rows with an as_of_at earlier than a
snapshot already taken, which silently changes what the same query
returns. A fabricated record that happens to be right today would be
indistinguishable from one that has since drifted.

Revision ID: d2b9f45c1a7e
Revises: c7e4a812b5d3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2b9f45c1a7e"
down_revision: Union[str, Sequence[str], None] = "c7e4a812b5d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.add_column(
        "market_snapshots",
        sa.Column("max_observation_age_seconds", sa.Integer(), nullable=True),
    )
    # The two counters are NOT NULL with a server_default so the ALTER can
    # run against a populated table without rewriting history: existing
    # snapshots applied no freshness gate, so "0 excluded" and "canonical
    # not stale" are true of them, not a guess.
    op.add_column(
        "market_snapshots",
        sa.Column("stale_books_excluded", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "market_snapshots",
        sa.Column("canonical_quote_stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "market_snapshots",
        sa.Column("selected_quotes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Drop the server defaults now that every existing row has a value.
    # Leaving them in place would let a future INSERT that forgets these
    # columns silently claim "nothing was excluded" -- the application is
    # the only thing that knows what a snapshot actually consumed.
    op.alter_column("market_snapshots", "stale_books_excluded", server_default=None)
    op.alter_column("market_snapshots", "canonical_quote_stale", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_column("market_snapshots", "selected_quotes")
    op.drop_column("market_snapshots", "canonical_quote_stale")
    op.drop_column("market_snapshots", "stale_books_excluded")
    op.drop_column("market_snapshots", "max_observation_age_seconds")
