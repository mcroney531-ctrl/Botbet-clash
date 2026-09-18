"""A week_number correction may not have NULL schedule provenance.

Revision ID: c94a06f2e5d7
Revises: b8d24f7e6c31
Create Date: 2026-09-18

The application refuses to write one, but the application is not the only
thing that can reach this table, and the whole point of the correction row
is that it is the durable justification for changing a field that is part
of a permanent identity scope. A correction that cannot name the schedule
snapshot behind it is indistinguishable from the hand-typed week it
replaces -- so the database refuses it too.

Scoped to `week_number` rather than applied to every correction: a future
correction of some other scope attribute may legitimately be justified by
something that is not a schedule fetch, and a constraint that would have
to be dropped to allow that is a constraint that will be dropped.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c94a06f2e5d7"
down_revision: Union[str, Sequence[str], None] = "b8d24f7e6c31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "week_correction_requires_schedule_call"


def upgrade() -> None:
    op.create_check_constraint(
        CONSTRAINT,
        "game_scope_corrections",
        "field_corrected <> 'week_number' OR schedule_provider_call_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "game_scope_corrections", type_="check")
