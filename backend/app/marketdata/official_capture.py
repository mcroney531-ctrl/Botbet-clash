"""The official checkpoint runner.

Resolves everything from the database:

    game_id -> Game.season_id -> active SeasonRules -> CapturePolicy
                              -> the production refresh for that provider

An operator running a real checkpoint cannot pass the tolerance, the retry
budget, the canonical sportsbook, the provider or the event id. All of them
change which market state reaches an irreversible capture, so all of them
come from frozen rules or from persisted identity. That is enforced
structurally: the CLI has no options for them and this module never
constructs a `CapturePolicy` directly.

**An official capture never runs without a refresh.** The first version of
this module called `run_official_checkpoint` with `refresh=None`, which
`run_checkpoint_cycle` treats as "no refresh supplied" and proceeds
straight to the capture. Since a capture is irreversible, that would have
consumed a real checkpoint on whatever stale quotes happened to be sitting
in Postgres — the freshness gate would have invalidated them, but the
checkpoint would be CAPTURED and gone. A real-provider season with no
production refresh wired now fails closed.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Callable, Sequence

from app.db.models.markets import Game
from app.db.session import session_scope
from app.marketdata.checkpoint_cycle import (
    CapturePolicy,
    CycleReport,
    RefreshCallable,
    RefreshOutcome,
    render,
    run_checkpoint_cycle,
)
from app.marketdata.game_refresh import refresh_game_market_data
from app.marketdata.providers.the_odds_api import PROVIDER_NAME as ODDS_PROVIDER


class NoProductionRefresh(RuntimeError):
    """No refresh implementation exists for this season's pinned provider.

    Fails closed rather than capturing. A capture is irreversible, so
    "we could not fetch anything, so we froze whatever was lying around"
    is not a recoverable outcome — it consumes the checkpoint.
    """


# Which provider pin has a production refresh. Adding a provider means
# adding its refresh here, not relaxing the check.
REFRESH_BUILDERS: dict[str, Callable[[uuid.UUID, CapturePolicy], RefreshCallable]] = {
    ODDS_PROVIDER: lambda game_id, policy: (
        lambda: refresh_game_market_data(
            game_id=game_id, market_data_provider=policy.market_data_provider
        )
    ),
}


def resolve_policy(*, game_id: uuid.UUID) -> CapturePolicy:
    with session_scope() as session:
        game = session.get(Game, game_id)
        if game is None:
            raise LookupError(f"game {game_id} not found")
        return CapturePolicy.from_season_rules(session, season_id=game.season_id)


def build_refresh(*, game_id: uuid.UUID, policy: CapturePolicy) -> RefreshCallable:
    builder = REFRESH_BUILDERS.get(policy.market_data_provider)
    if builder is None:
        raise NoProductionRefresh(
            f"no production refresh is wired for market_data_provider="
            f"{policy.market_data_provider!r}. Refusing to run an official capture: "
            "a capture is irreversible, so it must never be built from whatever "
            "quotes happen to already be persisted."
        )
    return builder(game_id, policy)


def run_official_checkpoint(
    *,
    game_id: uuid.UUID,
    checkpoint_type: str,
    owner: str | None = None,
    refresh: RefreshCallable | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> CycleReport:
    """One official capture cycle.

    `refresh` and `sleep_fn` exist only so a test can substitute a counter
    for the real network call and skip the real backoff. Neither is an
    operator-facing option, and there is no `now_fn` at all: an official
    capture must not be told what time it is.

    `refresh` is never optional in effect -- omitted, the production
    refresh for the season's pinned provider is built, and if none exists
    the run fails closed rather than capturing.
    """

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
        refresh=refresh if refresh is not None else build_refresh(game_id=game_id, policy=policy),
        owner=owner,
        sleep_fn=sleep_fn,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an official checkpoint capture")
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    parser.add_argument("--checkpoint-type", required=True, choices=["OPENING", "MID", "FINAL"])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and print the frozen policy. Makes ZERO provider calls and "
             "writes nothing.",
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
    # Built, not called: proves a refresh EXISTS for this provider without
    # spending anything.
    build_refresh(game_id=args.game_id, policy=policy)
    print("  production refresh           wired")
    if args.dry_run:
        print()
        print("DRY RUN — no provider calls, no writes, no cycle.")
        return 0

    report = run_official_checkpoint(game_id=args.game_id, checkpoint_type=args.checkpoint_type)
    print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
