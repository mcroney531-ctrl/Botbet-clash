"""Precommitted, asynchronously-resolved benchmark slate (ARCHITECTURE.md
§4a). Three phases now:

1. `commit_from_planned_fixtures` — THE core. Freezes a week's complete
   fixture pool and allocates slots from it. Knows nothing about
   providers, sessions of discovery, or `Game` rows.
2. `bind_fixture_to_game` — runs when the market provider finally posts an
   event and `game_registration` creates the `Game`.
3. `resolve_benchmark_slots_for_game` — runs once per game, at that
   game's own OPENING capture, and only ever touches that game's slots.

**There is ONE allocation implementation.** The Phase-2B `games=[...]`
entry point is now a thin synthetic adapter over the core rather than a
second slate writer. Two independently-correct writers would pass their
own tests for months and then disagree in the week it counted; the
adapter converts its input and delegates, so it cannot drift.

The core takes `PlannedFixture`s, never `Game`s. A `Game` exists only once
THE ODDS API has posted the event, so a pool made of `Game` rows let the
market provider's posting horizon decide a research sample — which is how
two unlisted Week-3 events could change a slate that nflverse already knew
all sixteen fixtures for.

UNFILLABLE slots are never reallocated (deliberate V1 choice — see
ARCHITECTURE.md §4a's rationale); they're reported as coverage, not
backfilled. A fixture the provider never lists is the same kind of event:
reported as a coverage failure, never substituted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.forecast_lab import (
    BenchmarkSlateFixture,
    BenchmarkSlatePlan,
    BenchmarkSlot,
)
from app.db.models.markets import Game
from app.db.models.season import Season
from app.db.repositories.benchmark_repository import BenchmarkRepository
from app.db.repositories.market_repository import MarketRepository
from app.domain.lines import is_pushable_line
from app.forecast_lab.fixture_identity import (
    FIXTURE_KEY_VERSION,
    REGULAR_SEASON,
    FixtureKey,
    PlannedFixture,
    pool_fingerprint,
)
from app.forecast_lab.slate_allocation import allocate
from app.rosterdata.teams import CanonicalTeam


class SlateBindingRefused(RuntimeError):
    """A planned fixture and a Game do not describe the same game."""


@dataclass(frozen=True, slots=True)
class PlanProvenance:
    """What makes a plan OFFICIAL and reproducible.

    Present only on the official path. A synthetic Phase-2B plan has no
    provider call behind it and says so by leaving this None, rather than
    by fabricating one — the database CHECK then refuses any plan flagged
    official without every field here.
    """

    rules_version: str
    schedule_provider_call_id: uuid.UUID
    resolver_version: str
    earliest_opening_at: datetime


def commit_from_planned_fixtures(
    session: Session,
    *,
    week_id: uuid.UUID,
    fixtures: Sequence[PlannedFixture],
    target_slot_count: int,
    prop_types: list[str],
    allocation_method: str,
    committed_at: datetime,
    provenance: PlanProvenance | None = None,
) -> BenchmarkSlatePlan:
    """Freeze the pool, allocate the slots. The only writer of a slate.

    Every fixture in `fixtures` is persisted, not just the selected ones.
    The pool is the thing that makes the selection auditable: without it,
    "which fixtures could this allocator have picked" is only answerable
    by re-fetching a schedule release that may since have changed.
    """

    if not fixtures:
        raise ValueError("cannot commit a benchmark slate plan for a week with no fixtures")
    if not prop_types:
        raise ValueError("cannot commit a benchmark slate plan with no prop types")

    ordered = tuple(sorted(fixtures, key=lambda f: f.key.value))
    repo = BenchmarkRepository(session)
    plan = repo.add_plan(BenchmarkSlatePlan(
        week_id=week_id,
        target_slot_count=target_slot_count,
        allocation_method=allocation_method,
        committed_at=committed_at,
        is_official=provenance is not None,
        rules_version=provenance.rules_version if provenance else None,
        schedule_provider_call_id=(
            provenance.schedule_provider_call_id if provenance else None
        ),
        resolver_version=provenance.resolver_version if provenance else None,
        fixture_key_version=FIXTURE_KEY_VERSION,
        fixture_pool_count=len(ordered),
        fixture_pool_fingerprint=pool_fingerprint(ordered),
        earliest_opening_at=provenance.earliest_opening_at if provenance else None,
    ))

    rows: dict[str, BenchmarkSlateFixture] = {}
    for fixture in ordered:
        row = BenchmarkSlateFixture(
            plan_id=plan.id,
            fixture_key=fixture.key.value,
            week_number=fixture.key.week,
            away_team_canonical=fixture.key.away.value,
            home_team_canonical=fixture.key.home.value,
            planned_kickoff_at=fixture.kickoff_at,
        )
        session.add(row)
        rows[fixture.key.value] = row
    session.flush()

    chosen = allocate(ordered, slots=target_slot_count, method=allocation_method)
    for i, fixture in enumerate(chosen):
        target_type = prop_types[i % len(prop_types)]
        # Deterministic fallback: the rest of the prop-type list, rotated
        # to start after the target type - fixed at commit time, never
        # decided at resolution time.
        rotation_point = (i % len(prop_types)) + 1
        fallback = prop_types[rotation_point:] + prop_types[:rotation_point - 1]
        repo.add_slot(BenchmarkSlot(
            plan_id=plan.id,
            slot_index=i + 1,
            slate_fixture_id=rows[fixture.key.value].id,
            target_stat_type=target_type,
            fallback_stat_types=fallback,
            status="PENDING",
        ))
    return plan


def planned_fixture_from_game(session: Session, game: Game) -> PlannedFixture:
    """A `Game` expressed in the ONE canonical fixture identity.

    Used by the synthetic adapter and by binding, so a test fixture and a
    production fixture are the same kind of thing.
    """

    season = session.get(Season, game.season_id)
    if season is None:
        raise LookupError(f"game {game.id} references a season that does not exist")
    return PlannedFixture(
        key=FixtureKey(
            season=season.year,
            game_type=REGULAR_SEASON,
            week=game.week_number,
            away=CanonicalTeam(game.away_team_canonical),
            home=CanonicalTeam(game.home_team_canonical),
        ),
        kickoff_at=game.kickoff_at,
    )


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
    """SYNTHETIC ADAPTER — Phase-2B ergonomics over the real core.

    Kept so the mocked-week acceptance test keeps exercising the whole
    12-step flow from `Game` rows, and kept as an ADAPTER rather than an
    implementation so it cannot grow a second opinion about how a slate is
    built. It converts, then delegates.

    Never the official path: it produces a plan with no provenance, which
    the database refuses to mark official.
    """

    if not games:
        raise ValueError("cannot commit a benchmark slate plan for a week with no games")

    fixtures = [planned_fixture_from_game(session, game) for game in games]
    plan = commit_from_planned_fixtures(
        session, week_id=week_id, fixtures=fixtures,
        target_slot_count=target_slot_count, prop_types=prop_types,
        allocation_method=allocation_method, committed_at=committed_at,
    )
    # The synthetic path's games already exist, so bind immediately. The
    # official path cannot do this: its fixtures have no Game yet.
    by_key = {planned_fixture_from_game(session, g).key.value: g for g in games}
    for row in session.execute(
        select(BenchmarkSlateFixture).where(BenchmarkSlateFixture.plan_id == plan.id)
    ).scalars():
        game = by_key.get(row.fixture_key)
        if game is not None:
            row.game_id = game.id
            row.bound_at = committed_at
    session.flush()
    return plan


def bind_fixture_to_game(
    session: Session,
    *,
    fixture: BenchmarkSlateFixture,
    game: Game,
    bound_at: datetime,
    kickoff_tolerance: timedelta,
) -> BenchmarkSlateFixture:
    """Attach a real `Game` to a planned fixture. Moves `game_id` ONLY.

    Binding is not reallocation and not a correction. The planned week,
    teams and kickoff are what the allocator actually saw; rewriting them
    to agree with a later schedule release would erase the record this row
    exists to keep. A disagreement is reported as drift and refused here.
    """

    planned = planned_fixture_from_game(session, game)
    if planned.key.value != fixture.fixture_key:
        raise SlateBindingRefused(
            f"planned fixture {fixture.fixture_key} does not match game "
            f"{planned.key.value}; binding never relabels a planned fixture"
        )
    drift = abs(game.kickoff_at - fixture.planned_kickoff_at)
    if drift > kickoff_tolerance:
        raise SlateBindingRefused(
            f"schedule drift on {fixture.fixture_key}: the plan recorded kickoff "
            f"{fixture.planned_kickoff_at.isoformat()}, the game says "
            f"{game.kickoff_at.isoformat()} ({drift.total_seconds() / 60:.0f}m "
            f"apart, tolerance {kickoff_tolerance.total_seconds() / 60:.0f}m). "
            "Reported, not resolved: the planned kickoff is not rewritten and "
            "the slot is not reallocated."
        )
    if fixture.game_id is not None and fixture.game_id != game.id:
        raise SlateBindingRefused(
            f"planned fixture {fixture.fixture_key} is already bound to game "
            f"{fixture.game_id}; a binding is not re-pointed"
        )
    fixture.game_id = game.id
    fixture.bound_at = bound_at
    session.flush()
    return fixture


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
