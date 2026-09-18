"""The official checkpoint runner.

Resolves everything from the database:

    game_id -> Game.season_id -> active SeasonRules -> CapturePolicy

An operator running a real checkpoint cannot pass the tolerance, the retry
budget, the canonical sportsbook or the provider. All four change which
market state reaches an irreversible capture, so all four are frozen rules
and a change is an amendment, never a flag. That is enforced structurally
here: the CLI has no options for them and this module never constructs a
`CapturePolicy` directly.

`CapturePolicy.from_season_rules` fails closed for a real-provider season
whose policy is not frozen, so an official run against the durable season
refuses until the amendment has been applied.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone
from typing import Sequence

from app.db.models.markets import Game
from app.db.session import session_scope
from app.marketdata.checkpoint_cycle import (
    CapturePolicy,
    CycleReport,
    RefreshCallable,
    render,
    run_checkpoint_cycle,
)


def resolve_policy(*, game_id: uuid.UUID) -> CapturePolicy:
    with session_scope() as session:
        game = session.get(Game, game_id)
        if game is None:
            raise LookupError(f"game {game_id} not found")
        return CapturePolicy.from_season_rules(session, season_id=game.season_id)


def run_official_checkpoint(
    *,
    game_id: uuid.UUID,
    checkpoint_type: str,
    refresh: RefreshCallable | None = None,
    owner: str | None = None,
) -> CycleReport:
    """One official capture cycle. No policy arguments, by construction."""

    policy = resolve_policy(game_id=game_id)
    if not policy.is_official:
        raise RuntimeError(
            f"season rules {policy.rules_version!r} do not carry a complete frozen "
            "capture policy; refusing to run an official checkpoint against an "
            "undecided policy"
        )
    return run_checkpoint_cycle(
        game_id=game_id,
        checkpoint_type=checkpoint_type,
        policy=policy,
        refresh=refresh,
        owner=owner,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an official checkpoint capture")
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    parser.add_argument("--checkpoint-type", required=True, choices=["OPENING", "MID", "FINAL"])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and print the frozen policy without running a cycle",
    )
    args = parser.parse_args(argv)

    policy = resolve_policy(game_id=args.game_id)
    print("=" * 66)
    print("OFFICIAL CAPTURE POLICY (resolved from frozen SeasonRules)")
    print("=" * 66)
    print(f"  rules_version                {policy.rules_version}")
    print(f"  is_official                  {policy.is_official}")
    print(f"  canonical_sportsbook         {policy.canonical_sportsbook}")
    print(f"  market_data_provider         {policy.market_data_provider}")
    print(f"  max_observation_age_seconds  {policy.max_observation_age_seconds}")
    print(f"  retry                        {policy.retry.as_record()}")
    if args.dry_run:
        print()
        print("DRY RUN — no cycle was run.")
        return 0

    report = run_official_checkpoint(game_id=args.game_id, checkpoint_type=args.checkpoint_type)
    print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
