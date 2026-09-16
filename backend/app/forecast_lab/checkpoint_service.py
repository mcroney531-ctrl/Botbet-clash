"""Kickoff-relative checkpoint capture (RULES.md §9, ARCHITECTURE.md §4).

This is the "callable job" version, not a scheduler: `capture_checkpoint`
does the work synchronously when called, using "now" as both the
eligibility check and (if eligible) the capture timestamp. A real
scheduler polling this repeatedly to land closer to each checkpoint's
`target_time` is future work — the important property proven here is
idempotency: calling this twice must never produce a second logical
checkpoint capture for the same (game, checkpoint_type).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models.markets import CheckpointRun, Game
from app.db.repositories.checkpoint_repository import CheckpointRepository
from app.db.repositories.market_repository import MarketRepository
from app.forecast_lab.evidence_service import create_evidence_snapshot, mock_evidence_payload
from app.forecast_lab.market_snapshot_service import MarketSnapshotService
from app.marketdata.provenance import SYNTHETIC_SOURCE


def compute_window(kickoff_at: datetime, checkpoint_type: str, windows_config: dict) -> tuple[datetime, datetime, datetime]:
    cfg = windows_config[checkpoint_type]
    window_start = kickoff_at - timedelta(hours=cfg["start_hours_before_kickoff"])
    window_end = kickoff_at - timedelta(hours=cfg["end_hours_before_kickoff"])
    target_offsets = {"OPENING": cfg["start_hours_before_kickoff"], "MID": 48, "FINAL": 3}
    target_time = kickoff_at - timedelta(hours=target_offsets[checkpoint_type])
    return window_start, window_end, target_time


def capture_checkpoint(
    session: Session,
    *,
    game: Game,
    checkpoint_type: str,
    windows_config: dict,
    now: datetime,
    canonical_sportsbook: str,
    devig_method: str = "PROPORTIONAL_V1",
    market_data_provider: str = SYNTHETIC_SOURCE,
) -> CheckpointRun:
    """Idempotent: a second call after `status == "CAPTURED"` is a pure
    no-op that returns the existing row untouched — no new snapshots, no
    new evidence, nothing re-processed."""

    checkpoint_repo = CheckpointRepository(session)
    run = checkpoint_repo.get(game.id, checkpoint_type)

    if run is not None and run.status == "CAPTURED":
        return run

    if run is None:
        window_start, window_end, target_time = compute_window(game.kickoff_at, checkpoint_type, windows_config)
        run = CheckpointRun(
            game_id=game.id,
            checkpoint_type=checkpoint_type,
            window_start=window_start,
            window_end=window_end,
            target_time=target_time,
            status="PENDING",
        )
        checkpoint_repo.add(run)

    if now < run.window_start:
        return run  # too early - stays PENDING
    if now > run.window_end:
        run.status = "MISSED"
        session.flush()
        return run

    market_repo = MarketRepository(session)
    snapshot_service = MarketSnapshotService(
        session, devig_method=devig_method, market_data_provider=market_data_provider
    )
    for market in market_repo.markets_for_game(game.id):
        snapshot = snapshot_service.build_snapshot(market_id=market.id, canonical_sportsbook=canonical_sportsbook, taken_at=now)
        create_evidence_snapshot(
            session,
            market_id=market.id,
            market_snapshot_id=snapshot.id,
            generated_at=now,
            payload=mock_evidence_payload(generated_at=now, market_snapshot=snapshot),
            checkpoint_run_id=run.id,
            checkpoint_type=checkpoint_type,
        )

    run.status = "CAPTURED"
    run.captured_at = now
    session.flush()
    return run
