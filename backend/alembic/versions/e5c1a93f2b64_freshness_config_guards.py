"""freshness config guards

Phase 4A.4. Additive plus two CHECK constraints that make the 4A.3
freshness columns answer for themselves.

`books_observed` is backfilled to `number_of_books + stale_books_excluded`,
which is true by definition for any row rather than only for rows written
without a gate. Backfilling to `number_of_books` alone would also have been
correct for every row that exists TODAY -- no gated capture has ever run --
but it would quietly bake that assumption into a migration that has to keep
working whenever it is replayed. The CHECK is added after the backfill, so
it validates the existing rows rather than being taken on trust.

Revision ID: e5c1a93f2b64
Revises: d2b9f45c1a7e
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e5c1a93f2b64"
down_revision: Union[str, Sequence[str], None] = "d2b9f45c1a7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.add_column(
        "market_snapshots",
        sa.Column("books_observed", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.execute(
        "UPDATE market_snapshots SET books_observed = number_of_books + stale_books_excluded"
    )
    # Dropped now that every row has a real value: leaving it would let an
    # INSERT that forgets the column claim "no books observed", which the
    # CHECK below would then reject in a far more confusing place.
    op.alter_column("market_snapshots", "books_observed", server_default=None)

    op.create_check_constraint(
        "max_observation_age_non_negative",
        "market_snapshots",
        "max_observation_age_seconds IS NULL OR max_observation_age_seconds >= 0",
    )
    op.create_check_constraint(
        "books_observed_accounts_for_every_book",
        "market_snapshots",
        "books_observed = number_of_books + stale_books_excluded",
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_constraint(
        "books_observed_accounts_for_every_book",
        "market_snapshots",
        type_="check",
    )
    op.drop_constraint(
        "max_observation_age_non_negative",
        "market_snapshots",
        type_="check",
    )
    op.drop_column("market_snapshots", "books_observed")
