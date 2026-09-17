"""roster identity

Phase 4A.2 of backend/docs/phase4-roster-identity-seam.md.

Mostly additive, with one deliberate constraint LOOSENING: Player.team and
Player.position become nullable. That is safe (NOT NULL -> NULL never
rejects an existing row) and it is the correct model -- a player's team is
game-scoped and now lives on game_players. Keeping those columns NOT NULL
would have forced placeholder roster values, which are prohibited, and the
orchestrator `opponent` bug is direct evidence of what happens when a
conveniently-populated legacy field gets treated as authoritative.

Revision ID: c7e4a812b5d3
Revises: b3f7c21d9e40
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7e4a812b5d3"
down_revision: Union[str, Sequence[str], None] = "b3f7c21d9e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYNTHETIC_SOURCE = "SYNTHETIC"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # ---------------------------------------------------------------
    # Canonical team backfill. Map ONLY explicitly known values; fail
    # loudly and name the offenders otherwise. Guessing here would write
    # a wrong team into a research row silently, which is precisely the
    # failure mode this whole seam exists to prevent.
    # ---------------------------------------------------------------
    from app.rosterdata.teams import CanonicalTeam

    known = {team.value for team in CanonicalTeam}
    existing = bind.execute(
        sa.text("SELECT DISTINCT home_team FROM games UNION SELECT DISTINCT away_team FROM games")
    ).fetchall()
    # Positional: a UNION does not carry a column alias through, so row.<name>
    # would hand back the Row itself rather than the scalar.
    unmapped = sorted({row[0] for row in existing} - known)
    if unmapped:
        raise RuntimeError(
            "Cannot add canonical team columns: these existing games.home_team / "
            f"away_team values are not in CanonicalTeam: {unmapped}.\n"
            "Add an explicit mapping in app/rosterdata/teams.py and re-run. This "
            "migration will not guess, substring-match, or default -- a wrong team "
            "silently corrupts every opponent derivation built on it."
        )

    op.add_column("games", sa.Column("home_team_canonical", sa.String(), nullable=True))
    op.add_column("games", sa.Column("away_team_canonical", sa.String(), nullable=True))
    # Existing rows already hold canonical-style codes (verified above), so the
    # backfill is a copy rather than a translation.
    op.execute(sa.text("UPDATE games SET home_team_canonical = home_team, away_team_canonical = away_team"))
    op.alter_column("games", "home_team_canonical", nullable=False)
    op.alter_column("games", "away_team_canonical", nullable=False)

    op.add_column("season_rules", sa.Column("roster_data_provider", sa.String(), nullable=True))
    op.execute(
        sa.text("UPDATE season_rules SET roster_data_provider = :src").bindparams(src=SYNTHETIC_SOURCE)
    )
    op.alter_column("season_rules", "roster_data_provider", nullable=False)

    # The loosening. Player is a person; team/position are game-scoped and
    # move to game_players.
    op.alter_column("players", "team", existing_type=sa.String(), nullable=True)
    op.alter_column("players", "position", existing_type=sa.String(), nullable=True)

    op.create_table(
        "game_players",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Uuid(), nullable=False),
        sa.Column("team", sa.String(), nullable=False),
        sa.Column("position", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_game_players_game_id_games")),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], name=op.f("fk_game_players_player_id_players")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_players")),
        sa.UniqueConstraint("game_id", "player_id", name=op.f("uq_game_players_game_id_player_id")),
    )

    op.create_table(
        "game_player_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_player_id", sa.Uuid(), nullable=False),
        sa.Column("roster_provider_call_id", sa.Uuid(), nullable=False),
        sa.Column("roster_season", sa.Integer(), nullable=False),
        sa.Column("roster_week", sa.Integer(), nullable=True),
        sa.Column("roster_basis", sa.String(), nullable=False),
        sa.Column("resolved_team", sa.String(), nullable=False),
        sa.Column("resolved_position", sa.String(), nullable=True),
        sa.Column("resolver_version", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "roster_basis IN ('CURRENT_CONTEMPORANEOUS','ARCHIVED_CONTEMPORANEOUS',"
            "'HISTORICAL_RECONSTRUCTED')",
            name=op.f("ck_game_player_observations_valid_roster_basis"),
        ),
        sa.ForeignKeyConstraint(
            ["game_player_id"], ["game_players.id"],
            name=op.f("fk_game_player_observations_game_player_id_game_players"),
        ),
        sa.ForeignKeyConstraint(
            ["roster_provider_call_id"], ["provider_calls.id"],
            name=op.f("fk_game_player_observations_roster_provider_call_id_provider_calls"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_player_observations")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("game_player_observations")
    op.drop_table("game_players")
    # Re-tightening requires every row to have a value; a downgrade on real
    # data would need a deliberate backfill first.
    op.alter_column("players", "position", existing_type=sa.String(), nullable=False)
    op.alter_column("players", "team", existing_type=sa.String(), nullable=False)
    op.drop_column("season_rules", "roster_data_provider")
    op.drop_column("games", "away_team_canonical")
    op.drop_column("games", "home_team_canonical")
