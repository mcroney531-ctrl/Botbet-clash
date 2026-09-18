"""Checkpoint window arithmetic and eligibility — pure, no session, no I/O.

Extracted in Phase 4A.5 so that "can this checkpoint do work right now"
has exactly ONE implementation, shared by:

    capture_checkpoint()          decides what to write
    preflight_checkpoint()        decides whether to spend a provider call

Two copies of this logic would be a cost-integrity bug waiting to happen:
the preflight could say ELIGIBLE, the capture could disagree, and the
difference would be paid for in Odds API credits on every scheduler tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class CheckpointDisposition(StrEnum):
    """What a checkpoint can do at a given instant."""

    ALREADY_CAPTURED = "ALREADY_CAPTURED"
    TOO_EARLY = "TOO_EARLY"
    EXPIRED = "EXPIRED"
    ELIGIBLE = "ELIGIBLE"

    @property
    def may_refresh(self) -> bool:
        """Whether a PAID market-data refresh is justified.

        Only ELIGIBLE. A CAPTURED checkpoint is immutable, so new quotes
        could not reach it; a too-early or expired one will not capture at
        all. Refreshing in any of those cases buys nothing and costs
        credits — and a scheduler retry loop would cost them repeatedly.
        """

        return self is CheckpointDisposition.ELIGIBLE


@dataclass(frozen=True, slots=True)
class CheckpointWindow:
    window_start: datetime
    window_end: datetime
    target_time: datetime

    def contains(self, at: datetime) -> bool:
        return self.window_start <= at <= self.window_end

    def seconds_remaining(self, at: datetime) -> float:
        return (self.window_end - at).total_seconds()


def compute_window(
    kickoff_at: datetime, checkpoint_type: str, windows_config: dict
) -> CheckpointWindow:
    cfg = windows_config[checkpoint_type]
    window_start = kickoff_at - timedelta(hours=cfg["start_hours_before_kickoff"])
    window_end = kickoff_at - timedelta(hours=cfg["end_hours_before_kickoff"])
    target_offsets = {"OPENING": cfg["start_hours_before_kickoff"], "MID": 48, "FINAL": 3}
    target_time = kickoff_at - timedelta(hours=target_offsets[checkpoint_type])
    return CheckpointWindow(window_start, window_end, target_time)


def disposition(
    *, status: str | None, window: CheckpointWindow, now: datetime
) -> CheckpointDisposition:
    """`status` is the existing CheckpointRun's status, or None when no run
    exists yet.

    CAPTURED is checked FIRST and independently of the window: a captured
    checkpoint is immutable whatever the clock says, and asking "is it
    still in its window" of a finished run is the kind of ordering slip
    that lets a late scheduler tick pay for a refresh that cannot land.
    """

    if status == "CAPTURED":
        return CheckpointDisposition.ALREADY_CAPTURED
    if now < window.window_start:
        return CheckpointDisposition.TOO_EARLY
    if now > window.window_end:
        return CheckpointDisposition.EXPIRED
    return CheckpointDisposition.ELIGIBLE
