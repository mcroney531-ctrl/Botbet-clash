"""Read-only threshold calibration CLI.

    python -m app.forecast_lab.calibrate_cli --game-id <uuid> \
        --taken-at 2026-09-17T20:10:56.225032+00:00 \
        --candidates 60,300,900,3600

Writes nothing, spends no provider credits, and does not touch a
`CheckpointRun`. Safe to point at the durable DET @ BUF game: that game's
FINAL capture is a one-shot, so experimenting with thresholds by capturing
it would permanently freeze experimental artifacts onto a real game.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime
from typing import Sequence

from app.db.session import session_scope
from app.forecast_lab.calibration import preview_thresholds, render

DEFAULT_CANDIDATES = "60,180,300,600,900,1800,3600,7200"


def _candidates(raw: str) -> list[int | None]:
    values: list[int | None] = [None]  # always score the ungated baseline
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value < 0:
            raise argparse.ArgumentTypeError(f"candidate {value} must be >= 0")
        values.append(value)
    return values


def _instant(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is naive. Give an explicit offset (e.g. ...+00:00) -- a "
            "capture time without a timezone silently shifts every observation age."
        )
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview freshness thresholds (read only)")
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--taken-at", required=True, type=_instant,
        help="the capture time to evaluate against, timezone-aware. This is a "
             "hypothetical captured_at, never a target_time.",
    )
    parser.add_argument("--canonical-sportsbook", default="DRAFTKINGS")
    parser.add_argument("--market-data-provider", default="THE_ODDS_API")
    parser.add_argument("--candidates", type=_candidates, default=_candidates(DEFAULT_CANDIDATES))
    args = parser.parse_args(argv)

    with session_scope() as session:
        report = preview_thresholds(
            session,
            game_id=args.game_id,
            taken_at=args.taken_at,
            canonical_sportsbook=args.canonical_sportsbook,
            market_data_provider=args.market_data_provider,
            candidates=args.candidates,
        )
        print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
