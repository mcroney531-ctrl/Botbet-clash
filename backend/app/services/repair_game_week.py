"""Explicit, audited correction of a game's `week_number`.

Silent repair is forbidden. Audited correction is not — and leaving a
known-false week in the durable research record forever is not better
integrity than correcting it in the open. It is just a different way to be
wrong, with no paper trail either way.

**The SCHEDULE decides the corrected week, never the operator.** The first
version of this module took `--authoritative-week 2` and trusted whoever
typed it. That reproduced, inside the repair tool, the exact defect the
repair exists to undo: Phase 4A.2 recorded DET @ BUF as week 3 because a
human typed `--week-number 3`. A correction sourced from a second human
guess is not a verification; it is the same mistake with a nicer audit
row. So the repair resolves the fixture itself, through the season's own
frozen pins, using the SAME matcher registration uses:

    Game -> Season -> active SeasonRules -> frozen roster_data_provider
         -> schedule implementation -> fetch schedule (persisted call)
         -> resolve_fixture(away, home, kickoff_at)

`--expect-authoritative-week` exists, but it is a GUARD, not an input: it
asserts what the operator believes the schedule will say and refuses when
the schedule disagrees. It can only ever cause the repair to do less.

What a correction may NEVER touch:

    Game.id          permanent
    external_ref     permanent provider identity
    home/away teams  identity
    kickoff_at       drives the checkpoint windows
    PropMarket ids   observations hang off them
    PropQuote ids    immutable observations

Dry run by default. `--apply` required.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.forecast_lab import AgentSession, EvidenceSnapshot, ForecastObservation
from app.db.models.markets import (
    CheckpointRun,
    Game,
    GameScopeCorrection,
    MarketSnapshot,
    PropMarket,
    PropQuote,
)
from app.db.session import session_scope
from app.marketdata.game_registration import _schedule_provider_for, _season_pins
from app.marketdata.telemetry import finish_run, record_call, start_run
from app.marketdata.week_resolution import (
    RESOLVER_VERSION,
    WeekOutcome,
    WeekResolution,
    resolve_fixture,
)
from app.rosterdata.teams import CanonicalTeam

FIELD_CORRECTED = "week_number"

DEFAULT_REASON = (
    "Phase 4A.2 acceptance game was manually assigned its week before "
    "authoritative schedule verification existed; the nflverse schedule "
    "resolves this fixture to a different week."
)


class RepairRefused(RuntimeError):
    """Nothing was written."""


# --------------------------------------------------------------------------
# 1. What the schedule says — derived, never supplied
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GameFixture:
    """The existing row, reconstructed as the resolver's input.

    Deliberately built from the PERSISTED game rather than from a fresh
    provider event. The repair is not rediscovering the fixture — the
    identity is correct and stays correct — it is asking the schedule what
    week that already-known fixture belongs to.
    """

    game_id: uuid.UUID
    external_ref: str
    season_id: uuid.UUID
    season_year: int
    roster_pin: str
    home: CanonicalTeam
    away: CanonicalTeam
    kickoff_at: datetime
    current_week: int


def load_fixture(session: Session, game_id: uuid.UUID) -> GameFixture:
    game = session.get(Game, game_id)
    if game is None:
        raise RepairRefused(f"game {game_id} not found")
    season, rules = _season_pins(session, game.season_id)
    return GameFixture(
        game_id=game.id,
        external_ref=game.external_ref,
        season_id=game.season_id,
        season_year=season.year,
        roster_pin=rules.roster_data_provider,
        home=CanonicalTeam(game.home_team_canonical),
        away=CanonicalTeam(game.away_team_canonical),
        kickoff_at=game.kickoff_at,
        current_week=game.week_number,
    )


@dataclass(frozen=True)
class ScheduleVerdict:
    """The schedule's answer plus the provider call that produced it."""

    fixture: GameFixture
    resolution: WeekResolution
    schedule_provider: str
    schedule_provider_call_id: uuid.UUID

    @property
    def authoritative_week(self) -> int | None:
        return self.resolution.week if self.resolution.resolved else None

    @property
    def drift_seconds(self) -> int | None:
        if self.resolution.scheduled_kickoff is None:
            return None
        delta = self.fixture.kickoff_at - self.resolution.scheduled_kickoff
        return int(abs(delta.total_seconds()))


def derive_authoritative_week(
    *,
    game_id: uuid.UUID,
    schedule_provider=None,
) -> ScheduleVerdict:
    """Ask the schedule what week this fixture is. Network, no lock held.

    Split from `plan_repair` so the ONE network call in this module happens
    outside every transaction — the same rule the capture cycle follows,
    and the reason `apply_repair` can hold a row lock at all.
    """

    with session_scope() as session:
        fixture = load_fixture(session, game_id)

    source = schedule_provider or _schedule_provider_for(fixture.roster_pin)
    provider_name = getattr(source, "provider_name", "?")
    result = source.fetch_schedule(season=fixture.season_year)

    # Persisted BEFORE the result is inspected, success or failure. A
    # correction that cannot name the schedule snapshot that justified it
    # is indistinguishable from the hand-typed week it replaces.
    with session_scope() as session:
        run = start_run(session, provider=provider_name, operation="FETCH_SCHEDULE")
        call = record_call(
            session, run=run, metadata=result.call_metadata, success=result.ok,
            error_category=result.error.category if result.error else None,
            error_message=result.error.message if result.error else None,
        )
        call_id = call.id
        finish_run(session, run=run, status="SUCCEEDED" if result.ok else "FAILED")

    if not result.ok:
        raise RepairRefused(
            f"schedule unavailable ({result.error.category}: {result.error.message}). "
            "Without the schedule there is no authoritative week, and this tool "
            "will not fall back to an operator-supplied one."
        )

    # THE SAME matcher registration uses, via the same module. A repair that
    # derived the week by a second route could disagree with the rule that
    # classified every other game in the season.
    resolution = resolve_fixture(
        home=fixture.home, away=fixture.away,
        kickoff_at=fixture.kickoff_at, schedule=result.payload,
    )
    return ScheduleVerdict(
        fixture=fixture, resolution=resolution,
        schedule_provider=provider_name, schedule_provider_call_id=call_id,
    )


# --------------------------------------------------------------------------
# 2. What already depends on the week label
# --------------------------------------------------------------------------


@dataclass
class DependencyCensus:
    """Counted, then CLASSIFIED. The first version blocked on any
    `MarketSnapshot` at all, which is too blunt: a snapshot is a derived
    read of quotes that hang off `game_id`, and `game_id` does not change.
    What makes an artifact unsafe to relabel is something having COMMITTED
    to it — an EvidenceSnapshot built from it, a forecast citing it, a
    model call that consumed it, a CAPTURED checkpoint declaring it
    official. A standalone snapshot nothing references is none of those.
    """

    prop_markets: int = 0
    prop_quotes: int = 0
    market_snapshots: int = 0
    snapshots_in_evidence: int = 0
    snapshots_in_agent_sessions: int = 0
    evidence_snapshots: int = 0
    forecast_observations: int = 0
    agent_sessions: int = 0
    checkpoints: dict[str, list[str]] = field(default_factory=dict)

    @property
    def referenced_snapshots(self) -> int:
        """Upper bound: the two reference sets may overlap, and for a
        blocking decision an over-count is the safe direction."""

        return max(self.snapshots_in_evidence, self.snapshots_in_agent_sessions)

    @property
    def standalone_snapshots(self) -> int:
        return max(self.market_snapshots - self.referenced_snapshots, 0)

    @property
    def captured_checkpoints(self) -> list[str]:
        return sorted(self.checkpoints.get("CAPTURED", []))


def take_census(session: Session, game_id: uuid.UUID) -> DependencyCensus:
    """Read-only. Every count is scoped through `PropMarket.game_id`."""

    census = DependencyCensus()
    markets = select(PropMarket.id).where(PropMarket.game_id == game_id).scalar_subquery()

    def count(model, column) -> int:
        return session.execute(
            select(func.count()).select_from(model).where(column.in_(markets))
        ).scalar() or 0

    census.prop_markets = session.execute(
        select(func.count()).select_from(PropMarket).where(PropMarket.game_id == game_id)
    ).scalar() or 0
    census.prop_quotes = count(PropQuote, PropQuote.market_id)
    census.market_snapshots = count(MarketSnapshot, MarketSnapshot.market_id)
    census.evidence_snapshots = count(EvidenceSnapshot, EvidenceSnapshot.market_id)
    census.forecast_observations = count(ForecastObservation, ForecastObservation.market_id)

    snapshots = (
        select(MarketSnapshot.id)
        .where(MarketSnapshot.market_id.in_(markets))
        .scalar_subquery()
    )
    census.snapshots_in_evidence = session.execute(
        select(func.count(func.distinct(EvidenceSnapshot.market_snapshot_id)))
        .where(EvidenceSnapshot.market_snapshot_id.in_(snapshots))
    ).scalar() or 0
    census.snapshots_in_agent_sessions = session.execute(
        select(func.count(func.distinct(AgentSession.market_snapshot_id)))
        .where(AgentSession.market_snapshot_id.in_(snapshots))
    ).scalar() or 0

    # An AgentSession has no game_id; it reaches this game through the
    # evidence or the snapshot it consumed. Either link means a model was
    # shown this game under its current week label.
    evidence = (
        select(EvidenceSnapshot.id)
        .where(EvidenceSnapshot.market_id.in_(markets))
        .scalar_subquery()
    )
    census.agent_sessions = session.execute(
        select(func.count()).select_from(AgentSession).where(
            AgentSession.evidence_snapshot_id.in_(evidence)
            | AgentSession.market_snapshot_id.in_(snapshots)
        )
    ).scalar() or 0

    for run in session.execute(
        select(CheckpointRun).where(CheckpointRun.game_id == game_id)
    ).scalars():
        census.checkpoints.setdefault(run.status, []).append(run.checkpoint_type)
    return census


def blockers_for(census: DependencyCensus) -> list[str]:
    """Which counted artifacts make the relabel unsafe, and why."""

    out: list[str] = []
    if census.captured_checkpoints:
        out.append(
            f"{len(census.captured_checkpoints)} CAPTURED checkpoint run(s): "
            + ", ".join(census.captured_checkpoints)
            + " — a capture is an official, irreversible declaration made "
            "under the current week label"
        )
    if census.evidence_snapshots:
        out.append(
            f"{census.evidence_snapshots} EvidenceSnapshot row(s) — immutable "
            "evidence assembled under the current week label"
        )
    if census.forecast_observations:
        out.append(
            f"{census.forecast_observations} ForecastObservation row(s) — "
            "competitive forecasts scored against this week"
        )
    if census.agent_sessions:
        out.append(
            f"{census.agent_sessions} AgentSession row(s) — a model was shown "
            "this game under the current week label"
        )
    if census.referenced_snapshots:
        out.append(
            f"{census.referenced_snapshots} MarketSnapshot row(s) referenced by "
            "evidence or an agent session"
        )
    return out


# --------------------------------------------------------------------------
# 3. Plan / apply
# --------------------------------------------------------------------------


@dataclass
class RepairPlan:
    verdict: ScheduleVerdict
    reason: str
    census: DependencyCensus
    blockers: list[str] = field(default_factory=list)

    @property
    def fixture(self) -> GameFixture:
        return self.verdict.fixture

    @property
    def safe(self) -> bool:
        return not self.blockers

    def render(self) -> str:
        f, v, c = self.fixture, self.verdict, self.census
        out = [
            "=" * 74,
            "GAME SCOPE CORRECTION — week_number",
            "=" * 74,
            f"  game_id       {f.game_id}",
            f"  external_ref  {f.external_ref}   (UNCHANGED)",
            f"  fixture       {f.away.value} @ {f.home.value}   (UNCHANGED)",
            f"  kickoff_at    {f.kickoff_at.isoformat()}   (UNCHANGED)",
            "",
            "  --- what the SCHEDULE says --------------------------------------",
            f"    provider          {v.schedule_provider}  (season pin "
            f"{f.roster_pin}, season {f.season_year})",
            f"    provider call     {v.schedule_provider_call_id}",
            f"    resolver          {RESOLVER_VERSION}",
            f"    outcome           {v.resolution.outcome}",
            f"    detail            {v.resolution.detail}",
            f"    kickoff drift     {v.drift_seconds}s"
            if v.drift_seconds is not None
            else "    kickoff drift     (schedule has no kickoff time)",
            "",
            f"  week_number   {f.current_week}  ->  {v.authoritative_week}",
            f"  reason        {self.reason}",
            "",
            "  --- dependency census ------------------------------------------",
            f"    prop markets                        {c.prop_markets}",
            f"    prop quotes                         {c.prop_quotes}",
            f"    market snapshots (total)            {c.market_snapshots}",
            f"      referenced by evidence            {c.snapshots_in_evidence}",
            f"      referenced by agent sessions      {c.snapshots_in_agent_sessions}",
            f"      standalone                        {c.standalone_snapshots}",
            f"    evidence snapshots                  {c.evidence_snapshots}",
            f"    forecast observations               {c.forecast_observations}",
            f"    agent sessions                      {c.agent_sessions}",
        ]
        for status in sorted(c.checkpoints):
            out.append(
                f"    checkpoints {status:<10}              "
                f"{', '.join(sorted(c.checkpoints[status]))}"
            )
        if not c.checkpoints:
            out.append("    checkpoints                         none")
        if self.blockers:
            out += ["", "  REFUSED — correcting the week would reinterpret:"]
            out += [f"    - {b}" for b in self.blockers]
            out += [
                "",
                "  This is no longer a mislabelled attribute; it is a dependency",
                "  graph. Bring it back for review rather than applying a simple",
                "  repair.",
            ]
        else:
            out += [
                "",
                "  Nothing has COMMITTED to the current week label: no CAPTURED",
                "  checkpoint, no evidence, no forecast, no agent session, and no",
                "  referenced snapshot. PropMarket / PropQuote / standalone",
                f"  MarketSnapshot rows ({c.standalone_snapshots}) hang off game_id,",
                "  which does not change, so no observation is reinterpreted.",
            ]
        out.append("=" * 74)
        return "\n".join(out)


def plan_repair(
    session: Session,
    *,
    verdict: ScheduleVerdict,
    expected_current_week: int,
    expect_authoritative_week: int | None = None,
    reason: str = DEFAULT_REASON,
) -> RepairPlan:
    """Validate the verdict against the row and the census. Writes nothing."""

    fixture = verdict.fixture
    game = session.get(Game, fixture.game_id)
    if game is None:
        raise RepairRefused(f"game {fixture.game_id} not found")

    # The operator must be repairing the row they reviewed.
    if game.week_number != expected_current_week:
        raise RepairRefused(
            f"expected week_number {expected_current_week} but the row says "
            f"{game.week_number}. Re-read the inspector before applying."
        )

    if not verdict.resolution.resolved:
        raise RepairRefused(
            f"the schedule did not resolve this fixture: "
            f"{verdict.resolution.outcome} — {verdict.resolution.detail}. "
            "This tool will not correct a week the schedule cannot confirm."
        )
    authoritative = verdict.authoritative_week
    if authoritative == game.week_number:
        raise RepairRefused(
            f"the schedule agrees the game is week {authoritative}; nothing to correct"
        )

    # A GUARD, never an input. It can only make the repair do less.
    if expect_authoritative_week is not None and expect_authoritative_week != authoritative:
        raise RepairRefused(
            f"--expect-authoritative-week {expect_authoritative_week} but the "
            f"{verdict.schedule_provider} schedule says week {authoritative}. The "
            "schedule decides; re-read it and re-run with the week it actually "
            "reports, or investigate why they disagree."
        )

    census = take_census(session, fixture.game_id)
    return RepairPlan(
        verdict=verdict, reason=reason, census=census, blockers=blockers_for(census),
    )


def apply_repair(
    session: Session,
    *,
    verdict: ScheduleVerdict,
    expected_current_week: int,
    expect_authoritative_week: int | None = None,
    reason: str = DEFAULT_REASON,
) -> GameScopeCorrection:
    """Lock the row, re-verify everything, then correct it. One transaction.

    The dry run and the apply are separate processes minutes apart, and the
    network call that produced `verdict` happened before either. Between
    them a capture could have landed, or another operator could have
    corrected the same row. So nothing observed earlier is trusted: the row
    is taken `FOR UPDATE` and the week check and the whole dependency census
    are re-run INSIDE the lock, against the locked row.

    No network happens here — the schedule was already fetched and its call
    already persisted — so the lock is bounded by local queries only.
    """

    game_id = verdict.fixture.game_id
    game = session.execute(
        select(Game).where(Game.id == game_id).with_for_update()
    ).scalar_one_or_none()
    if game is None:
        raise RepairRefused(f"game {game_id} not found")
    if game.week_number != expected_current_week:
        raise RepairRefused(
            f"the row moved while this repair was being prepared: expected week "
            f"{expected_current_week}, found {game.week_number}. Nothing written."
        )

    plan = plan_repair(
        session, verdict=verdict, expected_current_week=expected_current_week,
        expect_authoritative_week=expect_authoritative_week, reason=reason,
    )
    if not plan.safe:
        raise RepairRefused(
            "correcting the week would reinterpret existing artifacts: "
            + "; ".join(plan.blockers)
        )

    # Fail closed. A correction with no schedule call is exactly the
    # hand-typed week this tool exists to remove, and the database carries
    # the same requirement as a CHECK constraint.
    if verdict.schedule_provider_call_id is None:
        raise RepairRefused(
            "no schedule provider call was recorded for this verdict; a "
            f"{FIELD_CORRECTED} correction may not be written without one"
        )

    # The audit row is written FIRST and is append-only. If the update
    # failed after it, the record would still say a correction was
    # attempted; if the order were reversed and the audit insert failed,
    # the field would have moved with nothing explaining why.
    correction = GameScopeCorrection(
        game_id=game_id,
        field_corrected=FIELD_CORRECTED,
        old_value=str(game.week_number),
        new_value=str(plan.verdict.authoritative_week),
        schedule_provider_call_id=verdict.schedule_provider_call_id,
        resolver_version=RESOLVER_VERSION,
        reason=reason,
        corrected_at=datetime.now(timezone.utc),
    )
    session.add(correction)
    session.flush()

    game.week_number = plan.verdict.authoritative_week
    session.flush()
    return correction


# No post-correction `GameScopeObservation` is appended, deliberately.
# That table requires BOTH a market provider call and a schedule provider
# call, because a registration observation is a statement about an event we
# actually fetched. A repair fetches no event — the identity was never in
# doubt — so the honest choice is to leave the observation invariant alone
# rather than relax it to NOT NULL-optional to accommodate a row it was not
# designed for. `GameScopeCorrection` already carries the schedule call and
# the resolver version, which is the provenance a correction owes.


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audited correction of a game's week_number. The SCHEDULE "
                    "decides the corrected week; no flag can supply it.",
    )
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--expect-current-week", required=True, type=int,
        help="the week the inspector showed. The repair refuses if the row says "
             "anything else, so you correct the row you reviewed.",
    )
    parser.add_argument(
        "--expect-authoritative-week", type=int, default=None,
        help="OPTIONAL GUARD, not an override: the week you expect the schedule "
             "to report. If the schedule says anything else the repair refuses. "
             "It can never cause a week to be written that the schedule did not "
             "resolve on its own.",
    )
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually correct the row. WITHOUT this flag nothing is written "
             "except the schedule provider-call audit telemetry.",
    )
    args = parser.parse_args(argv)

    verdict = derive_authoritative_week(game_id=args.game_id)

    with session_scope() as session:
        plan = plan_repair(
            session, verdict=verdict,
            expected_current_week=args.expect_current_week,
            expect_authoritative_week=args.expect_authoritative_week,
            reason=args.reason,
        )
        print(plan.render())

    if not plan.safe:
        print()
        print("REFUSED — nothing written.")
        return 1
    if not args.apply:
        print()
        print("DRY RUN — no Game row written. Re-run with --apply to correct it.")
        return 0

    with session_scope() as session:
        correction = apply_repair(
            session, verdict=verdict,
            expected_current_week=args.expect_current_week,
            expect_authoritative_week=args.expect_authoritative_week,
            reason=args.reason,
        )
        print()
        print(f"APPLIED. Audit row {correction.id}: week_number "
              f"{correction.old_value} -> {correction.new_value} "
              f"(schedule call {correction.schedule_provider_call_id})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
