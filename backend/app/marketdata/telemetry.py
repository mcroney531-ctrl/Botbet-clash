"""Ingestion run / provider call audit records (seam doc §11).

Every external call attempt lands in `provider_calls`, successful or not.
This is where the money is measured: summing `quota_cost` answers "what
did this experiment actually cost" without estimating, and the newest
`quota_remaining` is what the reserve guard reads.

CREDENTIAL RULE, enforced by `sanitize_message` below: nothing written
here may contain a URL. This provider authenticates by query parameter,
so a URL is a credential.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ingestion import IngestionRun, ProviderCall
from app.marketdata.base import ProviderCallMetadata, ProviderDiagnostic

MINIMUM_CREDIT_RESERVE = 5000
"""Applied to historical/backfill calls only; the free-tier current-data
probe is exempt. There is no automatic bypass -- spending into the
reserve requires an explicit operator flag, recorded on the run."""

# Anything URL-shaped, plus bare key=value pairs that look like secrets.
# Deliberately blunt: a false positive costs a less readable error
# message, a false negative costs a leaked credential.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRETISH_RE = re.compile(
    r"(?i)\b(api[-_]?key|apikey|token|secret|authorization|password)\b\s*[=:]\s*\S+"
)


def sanitize_message(text: str) -> str:
    """Strip URLs and key-like assignments from a message.

    This is the last line of defence, not the first. Adapters are
    expected to construct safe messages in the first place rather than
    stringifying transport exceptions -- httpx embeds the full request
    URL, query string and all, in its exception text.
    """

    cleaned = _URL_RE.sub("[redacted-url]", text)
    cleaned = _SECRETISH_RE.sub("[redacted-credential]", cleaned)
    return cleaned


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def start_run(
    session: Session,
    *,
    provider: str,
    operation: str,
    sport: str | None = None,
    season_id: uuid.UUID | None = None,
    week_number: int | None = None,
    checkpoint_run_id: uuid.UUID | None = None,
    requested_stat_families: Sequence[str] | None = None,
    reserve_override: bool = False,
    now: datetime | None = None,
) -> IngestionRun:
    run = IngestionRun(
        provider=provider,
        operation=operation,
        sport=sport,
        season_id=season_id,
        week_number=week_number,
        checkpoint_run_id=checkpoint_run_id,
        requested_stat_families=(
            {"families": list(requested_stat_families)} if requested_stat_families else None
        ),
        status="RUNNING",
        reserve_override=reserve_override,
        started_at=now or datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()
    return run


def record_call(
    session: Session,
    *,
    run: IngestionRun,
    metadata: ProviderCallMetadata,
    success: bool,
    error_category: str | None = None,
    error_message: str | None = None,
    diagnostics: Sequence[ProviderDiagnostic] = (),
) -> ProviderCall:
    """Persist one call attempt and return it.

    The returned row's id is the provenance anchor every quote from this
    response points at, which is what makes replaying a stored response
    idempotent while a genuinely new poll records a new observation.
    """

    call = ProviderCall(
        ingestion_run_id=run.id,
        endpoint_capability=metadata.endpoint_capability,
        requested_at=metadata.requested_at,
        responded_at=metadata.responded_at,
        http_status=metadata.http_status,
        success=success,
        error_category=error_category,
        error_message=sanitize_message(error_message) if error_message else None,
        quota_used=metadata.quota_used,
        quota_remaining=metadata.quota_remaining,
        quota_cost=metadata.quota_cost,
        provider_request_id=metadata.provider_request_id,
        provider_snapshot_at=metadata.provider_snapshot_at,
        raw_response_body=metadata.raw_response_body,
        raw_response_sha256=metadata.raw_response_sha256,
        raw_response_bytes=metadata.raw_response_bytes,
        diagnostics=(
            {
                "entries": [
                    {
                        "category": d.category,
                        "detail": sanitize_message(d.detail),
                        "vendor_market_key": d.vendor_market_key,
                        "sportsbook": d.sportsbook,
                        "player_display_name": d.player_display_name,
                    }
                    for d in diagnostics
                ]
            }
            if diagnostics
            else None
        ),
    )
    session.add(call)
    session.flush()
    return call


def finish_run(
    session: Session,
    *,
    run: IngestionRun,
    status: str,
    now: datetime | None = None,
) -> IngestionRun:
    run.status = status
    run.finished_at = now or datetime.now(timezone.utc)
    session.flush()
    return run


def latest_quota_remaining(session: Session, *, provider: str) -> int | None:
    """Most recent observed `quota_remaining` for a provider, or None.

    Reads telemetry rather than estimating, which is the whole reason
    quota is recorded per response instead of per run.
    """

    stmt = (
        select(ProviderCall.quota_remaining)
        .join(IngestionRun, IngestionRun.id == ProviderCall.ingestion_run_id)
        .where(IngestionRun.provider == provider, ProviderCall.quota_remaining.isnot(None))
        .order_by(ProviderCall.requested_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def reserve_would_be_breached(
    session: Session, *, provider: str, reserve: int = MINIMUM_CREDIT_RESERVE
) -> bool:
    """True when a paid call should be refused.

    Unknown remaining credit is NOT treated as a breach: refusing every
    call before the first response would make the guard un-bootstrappable.
    The first paid call populates the telemetry the guard then reads.
    """

    remaining = latest_quota_remaining(session, provider=provider)
    if remaining is None:
        return False
    return remaining < reserve
