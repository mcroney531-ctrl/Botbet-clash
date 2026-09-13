"""Precommitted, asynchronously-resolved benchmark slate (ARCHITECTURE.md
§4a). Two phases:

1. `commit_benchmark_slate_plan` — runs once, before any game's OPENING
   window opens, using only the schedule and the prop-type list. Never
   looks at odds or forecasts.
2. `resolve_benchmark_slots_for_game` — runs once per game, at that
   game's own OPENING capture, and only ever touches that game's slots.

UNFILLABLE slots are never reallocated (deliberate V1 choice — see
ARCHITECTURE.md §4a's rationale); they're reported as coverage, not
backfilled.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.forecast_lab import BenchmarkSlatePlan, BenchmarkSlot
from app.db.models.markets import Game
from app.db.repositories.benchmark_repository import BenchmarkRepository
from app.db.repositories.market_repository import MarketRepository
from app.domain.lines import is_pushable_line


def commit_benchmark_slate_plan(
    session: Session,
    *,
    week_id: uuid.UUID,
    games: list[Game],
    target_slot_count: int,
    prop_types: list[str],
    committed_at: datetime,
    allocation_method: str = "ROUND_ROBIN_BY_KICKOFF_V0",
) -> BenchmarkSlatePlan:
    if not games:
        raise ValueError("cannot commit a benchmark slate plan for a week with no games")

    repo = BenchmarkRepository(session)
    plan = repo.add_plan(
        BenchmarkSlatePlan(week_id=week_id, target_slot_count=target_slot_count, allocation_method=allocation_method, committed_at=committed_at)
    )

    games_by_kickoff = sorted(games, key=lambda g: (g.kickoff_at, str(g.id)))
    for i in range(target_slot_count):
        game = games_by_kickoff[i % len(games_by_kickoff)]
        target_type = prop_types[i % len(prop_types)]
        # Deterministic fallback: the rest of the prop-type list, rotated
        # to start after the target type - fixed at commit time, never
        # decided at resolution time.
        rotation_point = (i % len(prop_types)) + 1
        fallback = prop_types[rotation_point:] + prop_types[:rotation_point - 1]
        repo.add_slot(
            BenchmarkSlot(
                plan_id=plan.id,
                slot_index=i + 1,
                game_id=game.id,
                target_stat_type=target_type,
                fallback_stat_types=fallback,
                status="PENDING",
            )
        )
    return plan


def resolve_benchmark_slots_for_game(session: Session, *, plan_id: uuid.UUID, game_id: uuid.UUID, resolved_at: datetime) -> list[BenchmarkSlot]:
    """Mechanical resolution: for each PENDING slot assigned to this game,
    walk `[target_stat_type] + fallback_stat_types` in order and take the
    first stat type with at least one eligible market, breaking ties by
    the lowest `player_id` (a fixed, arbitrary-but-deterministic rule —
    never "whichever looks interesting")."""

    benchmark_repo = BenchmarkRepository(session)
    market_repo = MarketRepository(session)
    slots = benchmark_repo.pending_slots_for_game(plan_id, game_id)
    markets = market_repo.markets_for_game(game_id)

    def is_eligible(market) -> bool:
        snapshot = market_repo.latest_snapshot(market.id)
        if snapshot is None or not snapshot.is_valid_canonical_baseline:
            return False
        if snapshot.canonical_line is None or is_pushable_line(snapshot.canonical_line):
            return False
        return True

    eligible_by_type: dict[str, list] = {}
    for market in markets:
        if is_eligible(market):
            eligible_by_type.setdefault(market.stat_type, []).append(market)
    for candidates in eligible_by_type.values():
        candidates.sort(key=lambda m: str(m.player_id))

    for slot in slots:
        chosen = None
        for stat_type in [slot.target_stat_type, *slot.fallback_stat_types]:
            candidates = eligible_by_type.get(stat_type)
            if candidates:
                chosen = candidates[0]
                break
        if chosen is not None:
            slot.resolved_market_id = chosen.id
            slot.status = "RESOLVED"
        else:
            slot.status = "UNFILLABLE"
        slot.resolved_at = resolved_at

    session.flush()
    return slots
