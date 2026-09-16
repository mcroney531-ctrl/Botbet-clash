"""Synthetic provenance for quotes that did not come from a provider.

`PropQuote.provider_call_id` is NOT NULL on purpose: every quote must be
traceable to the call that produced it (seam §5). Two populations of rows
cannot satisfy that honestly, and both are handled here rather than by
weakening the constraint:

1. Rows written before Phase 4 existed, backfilled by the migration.
2. Rows written by tests and by `app/ai/live_smoke.py`, which fabricate
   market state deliberately and have no provider behind them.

Both get a real, explicit row in `ingestion_runs` / `provider_calls`
marked SYNTHETIC. That keeps the constraint meaningful -- the provenance
chain always resolves -- while making the fabricated rows obvious in a
query rather than indistinguishable from real market data.

The IDs are fixed constants so the migration and the runtime helper
converge on the same singleton instead of racing to create two.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ingestion import IngestionRun, ProviderCall

SYNTHETIC_SOURCE = "SYNTHETIC"
SYNTHETIC_PARSER_VERSION = "legacy-v0"

SYNTHETIC_RUN_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")
SYNTHETIC_CALL_ID = uuid.UUID("00000000-0000-4000-8000-00000000f002")

# The epoch is a deliberate tell: a synthetic provenance record should
# never be mistaken for a real call that happened at a plausible time.
SYNTHETIC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def legacy_fingerprint(quote_id: uuid.UUID) -> str:
    """The documented exception to the normal fingerprint (seam §5).

    A real fingerprint hashes the provider call plus the normalized quote
    fields, which makes replaying an archived response idempotent. Rows
    that were never produced from a provider response cannot be replayed,
    so there is nothing for that fingerprint to protect against -- and
    hashing their contents would collide the moment two fabricated quotes
    happened to be identical. Deriving from the quote's own immutable id
    keeps the UNIQUE constraint satisfiable without pretending these rows
    have provenance they do not.
    """

    return hashlib.sha256(f"legacy:{quote_id}".encode()).hexdigest()


def ensure_synthetic_provenance(session: Session) -> uuid.UUID:
    """Get-or-create the singleton synthetic call; returns its id."""

    existing = session.execute(
        select(ProviderCall.id).where(ProviderCall.id == SYNTHETIC_CALL_ID)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run_exists = session.execute(
        select(IngestionRun.id).where(IngestionRun.id == SYNTHETIC_RUN_ID)
    ).scalar_one_or_none()
    if run_exists is None:
        session.add(
            IngestionRun(
                id=SYNTHETIC_RUN_ID,
                provider=SYNTHETIC_SOURCE,
                operation="SYNTHETIC_BACKFILL",
                status="SUCCEEDED",
                started_at=SYNTHETIC_EPOCH,
                finished_at=SYNTHETIC_EPOCH,
            )
        )
        session.flush()

    session.add(
        ProviderCall(
            id=SYNTHETIC_CALL_ID,
            ingestion_run_id=SYNTHETIC_RUN_ID,
            endpoint_capability="SYNTHETIC",
            requested_at=SYNTHETIC_EPOCH,
            responded_at=SYNTHETIC_EPOCH,
            success=True,
        )
    )
    session.flush()
    return SYNTHETIC_CALL_ID
