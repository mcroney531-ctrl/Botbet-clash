"""market data ingestion

Additive. Phase 4A.1 of backend/docs/phase4-ingestion-seam.md.

Three things happen here, and the third is the one that needs care:

1. Two new audit tables, `ingestion_runs` and `provider_calls`.
2. `prop_quotes` gains observation time and provenance; `prop_markets`
   gains its natural key; `season_rules` gains market-data source pinning.
3. Every pre-existing `prop_quotes` row is backfilled with explicit
   SYNTHETIC provenance, because `provider_call_id` is NOT NULL and those
   rows genuinely have no provider behind them. Making the column
   nullable to dodge this would have destroyed the guarantee the column
   exists for -- that every quote resolves to the call that produced it.

Revision ID: b3f7c21d9e40
Revises: a8d1acc96058
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b3f7c21d9e40"
down_revision: Union[str, Sequence[str], None] = "a8d1acc96058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYNTHETIC_RUN_ID = "00000000-0000-4000-8000-00000000f001"
SYNTHETIC_CALL_ID = "00000000-0000-4000-8000-00000000f002"
SYNTHETIC_SOURCE = "SYNTHETIC"
SYNTHETIC_PARSER_VERSION = "legacy-v0"
SYNTHETIC_EPOCH = "1970-01-01T00:00:00+00:00"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # ---------------------------------------------------------------
    # Guard first, before touching anything.
    #
    # The natural key added below cannot be created if duplicates already
    # exist, and the seam is explicit that we must not pick a winner:
    # silently merging or deleting a duplicated market would destroy real
    # quote history. Fail loudly and let a human decide instead.
    # ---------------------------------------------------------------
    duplicates = bind.execute(
        sa.text(
            """
            SELECT game_id, player_id, stat_type, COUNT(*) AS n
            FROM prop_markets
            GROUP BY game_id, player_id, stat_type
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if duplicates:
        listed = "\n".join(
            f"  game_id={r.game_id} player_id={r.player_id} stat_type={r.stat_type} count={r.n}"
            for r in duplicates
        )
        raise RuntimeError(
            "Cannot add the prop_markets natural key: duplicate "
            f"(game_id, player_id, stat_type) rows already exist.\n{listed}\n"
            "These must be reconciled deliberately -- this migration will not "
            "merge, delete, or choose a winner, because doing so would discard "
            "real quote history attached to the losing row."
        )

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("sport", sa.String(), nullable=True),
        sa.Column("season_id", sa.Uuid(), nullable=True),
        sa.Column("week_number", sa.Integer(), nullable=True),
        sa.Column("checkpoint_run_id", sa.Uuid(), nullable=True),
        sa.Column("requested_stat_families", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reserve_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("events_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quotes_observed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quotes_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quotes_deduplicated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("markets_quarantined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','PARTIAL','FAILED')",
            name=op.f("ck_ingestion_runs_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["checkpoint_run_id"], ["checkpoint_runs.id"],
            name=op.f("fk_ingestion_runs_checkpoint_run_id_checkpoint_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_ingestion_runs_season_id_seasons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
    )

    op.create_table(
        "provider_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_capability", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_category", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("quota_used", sa.BigInteger(), nullable=True),
        sa.Column("quota_remaining", sa.BigInteger(), nullable=True),
        sa.Column("quota_cost", sa.BigInteger(), nullable=True),
        sa.Column("provider_request_id", sa.String(), nullable=True),
        sa.Column("provider_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_response_body", sa.LargeBinary(), nullable=True),
        sa.Column("raw_response_sha256", sa.String(length=64), nullable=True),
        sa.Column("raw_response_bytes", sa.BigInteger(), nullable=True),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"], ["ingestion_runs.id"],
            name=op.f("fk_provider_calls_ingestion_run_id_ingestion_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_calls")),
    )

    # ---------------------------------------------------------------
    # The singleton synthetic provenance record. Created before the
    # prop_quotes backfill because every legacy row points at it, and
    # reused at runtime by MarketRepository.add_quote for fabricated
    # test/live_smoke quotes -- one mechanism, not two.
    # ---------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO ingestion_runs
                (id, provider, operation, status, started_at, finished_at, created_at)
            VALUES
                (CAST(:rid AS uuid), :src, 'SYNTHETIC_BACKFILL', 'SUCCEEDED',
                 CAST(:epoch AS timestamptz), CAST(:epoch AS timestamptz), CAST(:epoch AS timestamptz))
            """
        ).bindparams(rid=SYNTHETIC_RUN_ID, src=SYNTHETIC_SOURCE, epoch=SYNTHETIC_EPOCH)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_calls
                (id, ingestion_run_id, endpoint_capability, requested_at,
                 responded_at, success, created_at)
            VALUES
                (CAST(:cid AS uuid), CAST(:rid AS uuid), 'SYNTHETIC', CAST(:epoch AS timestamptz),
                 CAST(:epoch AS timestamptz), true, CAST(:epoch AS timestamptz))
            """
        ).bindparams(cid=SYNTHETIC_CALL_ID, rid=SYNTHETIC_RUN_ID, epoch=SYNTHETIC_EPOCH)
    )

    op.add_column("season_rules", sa.Column("market_data_provider", sa.String(), nullable=True))
    op.execute(
        sa.text("UPDATE season_rules SET market_data_provider = :src").bindparams(src=SYNTHETIC_SOURCE)
    )
    op.alter_column("season_rules", "market_data_provider", nullable=False)

    op.create_unique_constraint(
        op.f("uq_prop_markets_game_id_player_id_stat_type"),
        "prop_markets",
        ["game_id", "player_id", "stat_type"],
    )

    # Added nullable, backfilled, then tightened -- the standard three-step
    # for a NOT NULL column on a populated table.
    op.add_column("prop_quotes", sa.Column("as_of_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("prop_quotes", sa.Column("provider_market_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("prop_quotes", sa.Column("source", sa.String(), nullable=True))
    op.add_column("prop_quotes", sa.Column("provider_call_id", sa.Uuid(), nullable=True))
    op.add_column("prop_quotes", sa.Column("ingestion_run_id", sa.Uuid(), nullable=True))
    op.add_column("prop_quotes", sa.Column("parser_version", sa.String(), nullable=True))
    op.add_column("prop_quotes", sa.Column("fingerprint", sa.String(length=64), nullable=True))

    # as_of_at = retrieved_at is CORRECT for these rows, not a convenient
    # default: every quote written before Phase 4 came from a fabricated
    # "current" pull, where the observation time and the retrieval time
    # genuinely are the same instant.
    #
    # The fingerprint is the documented legacy exception (seam §5). A real
    # fingerprint hashes the provider call plus the quote fields so that
    # replaying an archived response is idempotent; these rows were never
    # produced from a provider response and cannot be replayed, and hashing
    # their contents would collide the moment two fabricated quotes matched.
    # Deriving from the row's own immutable id satisfies UNIQUE without
    # pretending the row has provenance it does not.
    op.execute(
        sa.text(
            """
            UPDATE prop_quotes
            SET as_of_at         = retrieved_at,
                source           = :src,
                provider_call_id = CAST(:cid AS uuid),
                ingestion_run_id = CAST(:rid AS uuid),
                parser_version   = :pv,
                fingerprint      = encode(sha256(('legacy:' || id::text)::bytea), 'hex')
            """
        ).bindparams(
            src=SYNTHETIC_SOURCE,
            cid=SYNTHETIC_CALL_ID,
            rid=SYNTHETIC_RUN_ID,
            pv=SYNTHETIC_PARSER_VERSION,
        )
    )

    op.alter_column("prop_quotes", "as_of_at", nullable=False)
    op.alter_column("prop_quotes", "source", nullable=False)
    op.alter_column("prop_quotes", "provider_call_id", nullable=False)
    op.alter_column("prop_quotes", "parser_version", nullable=False)
    op.alter_column("prop_quotes", "fingerprint", nullable=False)

    op.create_foreign_key(
        op.f("fk_prop_quotes_provider_call_id_provider_calls"),
        "prop_quotes", "provider_calls", ["provider_call_id"], ["id"],
    )
    op.create_foreign_key(
        op.f("fk_prop_quotes_ingestion_run_id_ingestion_runs"),
        "prop_quotes", "ingestion_runs", ["ingestion_run_id"], ["id"],
    )
    op.create_unique_constraint(op.f("uq_prop_quotes_fingerprint"), "prop_quotes", ["fingerprint"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("uq_prop_quotes_fingerprint"), "prop_quotes", type_="unique")
    op.drop_constraint(op.f("fk_prop_quotes_ingestion_run_id_ingestion_runs"), "prop_quotes", type_="foreignkey")
    op.drop_constraint(op.f("fk_prop_quotes_provider_call_id_provider_calls"), "prop_quotes", type_="foreignkey")
    for column in (
        "fingerprint", "parser_version", "ingestion_run_id", "provider_call_id",
        "source", "provider_market_updated_at", "as_of_at",
    ):
        op.drop_column("prop_quotes", column)
    op.drop_constraint(op.f("uq_prop_markets_game_id_player_id_stat_type"), "prop_markets", type_="unique")
    op.drop_column("season_rules", "market_data_provider")
    op.drop_table("provider_calls")
    op.drop_table("ingestion_runs")
