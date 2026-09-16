"""Market-data ingestion audit trail (backend/docs/phase4-ingestion-seam.md §11).

Two tables, and the split matters: `IngestionRun` is one logical
operation, `ProviderCall` is one external call *attempt* -- including
attempts that die before an HTTP response exists, which is why almost
every response-derived column here is nullable.

`ProviderCall` is also the provenance anchor for every `PropQuote`
(§5): quotes point at the exact call that produced them, so replaying a
stored response is idempotent while a genuinely new poll legitimately
records a new observation.

NOTHING in this module may ever hold a credential. No request URL, no
query string, no Authorization header -- see the note on
`error_message`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, uuid_pk

INGESTION_RUN_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED")


class IngestionRun(Base):
    """One logical ingestion operation. A validation probe is a run too
    (operation=VALIDATION_PROBE), which is why season_id/week_number are
    nullable -- the probe has neither."""

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    sport: Mapped[str | None] = mapped_column(String, nullable=True)
    season_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    week_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("checkpoint_runs.id"), nullable=True)
    requested_stat_families: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    # Spending into MINIMUM_CREDIT_RESERVE is never automatic (§11.2). When
    # an operator forces it, that fact is recorded here rather than left to
    # a log line, so the protection cannot quietly disappear.
    reserve_override: Mapped[bool] = mapped_column(nullable=False, default=False)
    events_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes_deduplicated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    markets_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diagnostics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','PARTIAL','FAILED')",
            name="valid_status",
        ),
    )


class ProviderCall(Base):
    """One external provider-call attempt, successful or not.

    `raw_response_body` is LargeBinary rather than JSONB on purpose: the
    seam (§11.1) retains the response *as received* so a future parsing
    correction can be diagnosed against the real bytes. Parsing and
    re-serializing into JSONB would defeat the point -- the stored copy
    would be our interpretation, not the provider's output -- and the
    sha256 must cover exactly what arrived on the wire.
    """

    __tablename__ = "provider_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    # Our capability name (LIST_EVENTS / FETCH_QUOTES / FETCH_HISTORICAL),
    # never a vendor path and never a URL -- a URL is exactly where this
    # provider's credential travels.
    endpoint_capability: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(nullable=False, default=False)
    error_category: Mapped[str | None] = mapped_column(String, nullable=True)
    # MUST be a normalized, sanitized message. Never str(exc) from an HTTP
    # client: httpx embeds the full request URL -- including the apiKey
    # query parameter -- in its transport exception strings.
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    quota_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quota_remaining: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quota_cost: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_snapshot_at: Mapped[datetime | None] = mapped_column(nullable=True)
    raw_response_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    raw_response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_response_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    diagnostics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
