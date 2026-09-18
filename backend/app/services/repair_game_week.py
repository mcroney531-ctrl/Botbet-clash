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
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.competition import (
    PassDecision,
    StakeRecommendation,
    Ticket,
    Wager,
)
from app.db.models.forecast_lab import (
    AgentSession,
    AgentSessionEvidenceSnapshot,
    BenchmarkSlatePlan,
    BenchmarkSlot,
    EvidenceSnapshot,
    ForecastObservation,
)
from app.db.models.markets import (
    CheckpointRun,
    Game,
    GameScopeCorrection,
    MarketSnapshot,
    PropMarket,
    PropQuote,
)
from app.db.models.roster import GamePlayer
from app.db.models.season import Week
from app.db.models.settlement import (
    BankrollTransaction,
    ResearchSettlement,
    Settlement,
)
from app.db.session import session_scope
from app.marketdata.game_registration import (
    ScheduleSourceUnavailable,
    _schedule_provider_for,
    _season_pins,
)
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
#
# The first version counted five models and called the result "the whole
# dependency census". It was not. Every competitive artifact below can
# reach this game and carry its own week interpretation, and correcting
# `Game.week_number` underneath one of them produces exactly the
# cross-week contradiction this repair exists to eliminate:
#
#     Game.week_number = 2   while   Ticket.week_id -> Week 3
#
# Two specific holes are worth naming, because both were silent:
#
#   * `agent_session_evidence_snapshots` is the source of truth for
#     multi-market and batched model inputs -- `create_pending` says so in
#     as many words, and it never populates `AgentSession`'s singular
#     `evidence_snapshot_id` / `market_snapshot_id` at all. Counting only
#     those two columns therefore reported `agent_sessions = 0` for EVERY
#     batched call that consumed this game.
#
#   * `max(count_a, count_b)` was labelled an upper bound on the union of
#     two snapshot-reference sets. It is a LOWER bound: two disjoint sets
#     of 2 and 3 reference five snapshots, not three. That understated
#     `referenced_snapshots` and therefore OVERSTATED
#     `standalone_snapshots` -- the number the safe/unsafe call reads.


class Reach(StrEnum):
    """How a table relates to a game's week label."""

    BLOCKING = "BLOCKING"
    """Durable competition or research state whose interpretation changes."""

    OBSERVATIONAL = "OBSERVATIONAL"
    """Hangs off game_id or market_id, which do not change. Raw or derived
    observation that commits nothing."""

    AUDIT = "AUDIT"
    """Our own provenance and lease rows. Describe the repair; never gate it."""


# EVERY table that can reach `games` by foreign key, classified. This is a
# declaration, not documentation: `test_every_table_that_can_reach_a_game_is_classified`
# recomputes the FK closure from the live metadata and fails if a table is
# missing, so a model added later cannot quietly re-open the hole that made
# this pass necessary. Adding a row here is a deliberate act.
CENSUS_CLASSIFICATION: dict[str, Reach] = {
    # -- raw and derived observation ----------------------------------
    "prop_markets": Reach.OBSERVATIONAL,
    "prop_quotes": Reach.OBSERVATIONAL,
    "market_snapshots": Reach.OBSERVATIONAL,  # unless referenced; see below
    "game_players": Reach.OBSERVATIONAL,
    "game_player_observations": Reach.OBSERVATIONAL,
    "checkpoint_runs": Reach.OBSERVATIONAL,  # unless CAPTURED; see below
    # -- our own provenance -------------------------------------------
    "game_scope_observations": Reach.AUDIT,
    "game_scope_corrections": Reach.AUDIT,
    "checkpoint_cycle_leases": Reach.AUDIT,
    # Reached because `IngestionRun.checkpoint_run_id` points at a
    # checkpoint. Provider-call telemetry describes what we FETCHED, never
    # what we concluded, so it carries no week interpretation to
    # contradict. Found by the closure check, not by hand -- which is the
    # point of computing it.
    "ingestion_runs": Reach.AUDIT,
    "provider_calls": Reach.AUDIT,
    # -- committed research state -------------------------------------
    "evidence_snapshots": Reach.BLOCKING,
    "forecast_observations": Reach.BLOCKING,
    "agent_sessions": Reach.BLOCKING,
    "agent_session_evidence_snapshots": Reach.BLOCKING,
    "research_settlements": Reach.BLOCKING,
    "benchmark_slots": Reach.BLOCKING,
    # -- committed competition state ----------------------------------
    "stake_recommendations": Reach.BLOCKING,
    "tickets": Reach.BLOCKING,
    "wagers": Reach.BLOCKING,
    "pass_decisions": Reach.BLOCKING,
    "settlements": Reach.BLOCKING,
    "bankroll_transactions": Reach.BLOCKING,
}


def tables_reaching_a_game(metadata=None) -> set[str]:
    """Transitive FK closure: every table that can reach `games`.

    Computed from the live metadata rather than listed by hand, so the
    completeness check cannot drift from the schema. Direction is
    child-ward -- a table is in the closure if it REFERENCES something
    already in it -- which is what "this row is about that game" means.
    """

    from app.db.base import Base

    md = metadata if metadata is not None else Base.metadata
    reached = {"games"}
    changed = True
    while changed:
        changed = False
        for table in md.tables.values():
            if table.name in reached:
                continue
            targets = {fk.column.table.name for fk in table.foreign_keys}
            if targets & reached:
                reached.add(table.name)
                changed = True
    return reached - {"games"}


@dataclass(frozen=True)
class ArtifactCount:
    """One classified row in the census."""

    label: str
    count: int
    blocking: bool
    detail: str = ""

    def render(self) -> str:
        line = f"    {self.label:<34}{self.count:>6}"
        if self.detail:
            line += f"   {self.detail}"
        return line


@dataclass
class DependencyCensus:
    """Counted, then CLASSIFIED. Blocking is not "an artifact exists"; it
    is "something COMMITTED to this game under its current week label".
    A standalone snapshot nothing references, a PENDING checkpoint, a
    roster observation and a quote are all observation -- they hang off
    `game_id` / `market_id`, neither of which the repair touches."""

    prop_markets: int = 0
    prop_quotes: int = 0
    game_players: int = 0
    market_snapshots: int = 0
    snapshots_in_evidence: int = 0
    snapshots_in_agent_sessions: int = 0
    snapshots_in_tickets: int = 0
    referenced_snapshots: int = 0
    evidence_snapshots: int = 0
    forecast_observations: int = 0
    agent_sessions: int = 0
    batched_agent_sessions: int = 0
    benchmark_slots: int = 0
    stake_recommendations: int = 0
    tickets: int = 0
    wagers: int = 0
    pass_decisions: int = 0
    research_settlements: int = 0
    settlements: int = 0
    bankroll_transactions: int = 0
    checkpoints: dict[str, list[str]] = field(default_factory=dict)
    committed_weeks: dict[str, list[int]] = field(default_factory=dict)

    @property
    def standalone_snapshots(self) -> int:
        return max(self.market_snapshots - self.referenced_snapshots, 0)

    @property
    def captured_checkpoints(self) -> list[str]:
        return sorted(self.checkpoints.get("CAPTURED", []))

    def _weeks(self, label: str) -> str:
        weeks = self.committed_weeks.get(label)
        return f"naming week(s) {weeks}" if weeks else ""

    def rows(self) -> list[ArtifactCount]:
        """Every counted artifact, blocking flag included. Reported in
        full even at zero: "we looked and found none" and "we never
        looked" must not render identically."""

        captured = self.captured_checkpoints
        return [
            ArtifactCount("prop markets", self.prop_markets, False),
            ArtifactCount("prop quotes", self.prop_quotes, False),
            ArtifactCount("game players", self.game_players, False),
            ArtifactCount("market snapshots (total)", self.market_snapshots, False),
            ArtifactCount("  referenced by evidence", self.snapshots_in_evidence, False),
            ArtifactCount("  referenced by agent sessions", self.snapshots_in_agent_sessions, False),
            ArtifactCount("  referenced by tickets", self.snapshots_in_tickets, False),
            ArtifactCount("  referenced (DISTINCT union)", self.referenced_snapshots,
                          self.referenced_snapshots > 0),
            ArtifactCount("  standalone", self.standalone_snapshots, False),
            ArtifactCount("CAPTURED checkpoint runs", len(captured), bool(captured),
                          ", ".join(captured)),
            ArtifactCount("evidence snapshots", self.evidence_snapshots,
                          self.evidence_snapshots > 0),
            ArtifactCount("forecast observations", self.forecast_observations,
                          self.forecast_observations > 0),
            ArtifactCount("agent sessions (any route)", self.agent_sessions,
                          self.agent_sessions > 0,
                          f"{self.batched_agent_sessions} via the batch join table"),
            ArtifactCount("benchmark slots", self.benchmark_slots,
                          self.benchmark_slots > 0, self._weeks("benchmark_slots")),
            ArtifactCount("stake recommendations", self.stake_recommendations,
                          self.stake_recommendations > 0),
            ArtifactCount("tickets", self.tickets, self.tickets > 0, self._weeks("tickets")),
            ArtifactCount("wagers", self.wagers, self.wagers > 0, self._weeks("wagers")),
            ArtifactCount("pass decisions", self.pass_decisions,
                          self.pass_decisions > 0, self._weeks("pass_decisions")),
            ArtifactCount("research settlements", self.research_settlements,
                          self.research_settlements > 0),
            ArtifactCount("settlements", self.settlements, self.settlements > 0),
            ArtifactCount("bankroll transactions", self.bankroll_transactions,
                          self.bankroll_transactions > 0),
        ]


def take_census(session: Session, game_id: uuid.UUID) -> DependencyCensus:
    """Read-only. Every count is scoped through `PropMarket.game_id` or
    `game_id` directly, and every BLOCKING table in
    `CENSUS_CLASSIFICATION` is queried."""

    c = DependencyCensus()
    markets = select(PropMarket.id).where(PropMarket.game_id == game_id).scalar_subquery()
    snapshots = (
        select(MarketSnapshot.id).where(MarketSnapshot.market_id.in_(markets)).scalar_subquery()
    )
    evidence = (
        select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id.in_(markets)).scalar_subquery()
    )

    def count(model, *where) -> int:
        return session.execute(
            select(func.count()).select_from(model).where(*where)
        ).scalar() or 0

    def weeks_named(model, *where) -> list[int]:
        """Which weeks the committed artifacts actually name. A contradiction
        an operator can SEE beats a bare count they have to go look up."""

        return sorted({
            w for (w,) in session.execute(
                select(Week.week_number).distinct()
                .select_from(model).join(Week, Week.id == model.week_id).where(*where)
            )
        })

    # -- observation --------------------------------------------------
    c.prop_markets = count(PropMarket, PropMarket.game_id == game_id)
    c.prop_quotes = count(PropQuote, PropQuote.market_id.in_(markets))
    c.game_players = count(GamePlayer, GamePlayer.game_id == game_id)
    c.market_snapshots = count(MarketSnapshot, MarketSnapshot.market_id.in_(markets))

    for run in session.execute(
        select(CheckpointRun).where(CheckpointRun.game_id == game_id)
    ).scalars():
        c.checkpoints.setdefault(run.status, []).append(run.checkpoint_type)

    # -- committed research -------------------------------------------
    c.evidence_snapshots = count(EvidenceSnapshot, EvidenceSnapshot.market_id.in_(markets))
    c.forecast_observations = count(
        ForecastObservation, ForecastObservation.market_id.in_(markets)
    )

    # An AgentSession has no game_id. It reaches this game three ways, and
    # the BATCH ROUTE IS THE NORMAL ONE: `create_pending` populates only
    # the join table, leaving both singular columns NULL. DISTINCT over the
    # union, because one session can arrive by more than one route.
    batched = (
        select(AgentSessionEvidenceSnapshot.agent_session_id)
        .where(
            AgentSessionEvidenceSnapshot.market_id.in_(markets)
            | AgentSessionEvidenceSnapshot.evidence_snapshot_id.in_(evidence)
        )
        .scalar_subquery()
    )
    c.agent_sessions = count(
        AgentSession,
        AgentSession.evidence_snapshot_id.in_(evidence)
        | AgentSession.market_snapshot_id.in_(snapshots)
        | AgentSession.id.in_(batched),
    )
    c.batched_agent_sessions = count(AgentSession, AgentSession.id.in_(batched))

    c.research_settlements = count(
        ResearchSettlement, ResearchSettlement.market_id.in_(markets)
    )
    # Either link counts: a slot names the game directly, and a resolved
    # slot also names the market it picked inside it.
    slot_where = (
        (BenchmarkSlot.game_id == game_id) | (BenchmarkSlot.resolved_market_id.in_(markets))
    )
    c.benchmark_slots = count(BenchmarkSlot, slot_where)
    c.committed_weeks["benchmark_slots"] = sorted({
        w for (w,) in session.execute(
            select(Week.week_number).distinct().select_from(BenchmarkSlot)
            .join(BenchmarkSlatePlan, BenchmarkSlatePlan.id == BenchmarkSlot.plan_id)
            .join(Week, Week.id == BenchmarkSlatePlan.week_id)
            .where(slot_where)
        )
    })

    # -- committed competition ----------------------------------------
    c.stake_recommendations = count(
        StakeRecommendation, StakeRecommendation.market_id.in_(markets)
    )
    ticket_where = (
        Ticket.market_id.in_(markets) | Ticket.market_snapshot_id.in_(snapshots)
    )
    c.tickets = count(Ticket, ticket_where)
    c.committed_weeks["tickets"] = weeks_named(Ticket, ticket_where)

    c.wagers = count(Wager, Wager.market_id.in_(markets))
    c.committed_weeks["wagers"] = weeks_named(Wager, Wager.market_id.in_(markets))

    c.pass_decisions = count(
        PassDecision, PassDecision.best_available_candidate_market_id.in_(markets)
    )
    c.committed_weeks["pass_decisions"] = weeks_named(
        PassDecision, PassDecision.best_available_candidate_market_id.in_(markets)
    )

    # Two hops out, and the most committed artifacts in the system: real
    # money moved against a wager on a market in this game.
    wagers = select(Wager.id).where(Wager.market_id.in_(markets)).scalar_subquery()
    c.settlements = count(Settlement, Settlement.wager_id.in_(wagers))
    c.bankroll_transactions = count(
        BankrollTransaction, BankrollTransaction.wager_id.in_(wagers)
    )

    # -- snapshot classification --------------------------------------
    #
    # A TRUE DISTINCT UNION, not max() of the parts. `MarketSnapshot.id` is
    # the primary key, so counting matching ROWS is exactly the cardinality
    # of the union of the reference sets.
    c.snapshots_in_evidence = session.execute(
        select(func.count(func.distinct(EvidenceSnapshot.market_snapshot_id)))
        .where(EvidenceSnapshot.market_snapshot_id.in_(snapshots))
    ).scalar() or 0
    c.snapshots_in_agent_sessions = session.execute(
        select(func.count(func.distinct(AgentSession.market_snapshot_id)))
        .where(AgentSession.market_snapshot_id.in_(snapshots))
    ).scalar() or 0
    c.snapshots_in_tickets = session.execute(
        select(func.count(func.distinct(Ticket.market_snapshot_id)))
        .where(Ticket.market_snapshot_id.in_(snapshots))
    ).scalar() or 0

    referencing_evidence = select(EvidenceSnapshot.market_snapshot_id).scalar_subquery()
    referencing_sessions = (
        select(AgentSession.market_snapshot_id)
        .where(AgentSession.market_snapshot_id.is_not(None))
        .scalar_subquery()
    )
    referencing_tickets = (
        select(Ticket.market_snapshot_id)
        .where(Ticket.market_snapshot_id.is_not(None))
        .scalar_subquery()
    )
    c.referenced_snapshots = count(
        MarketSnapshot,
        MarketSnapshot.market_id.in_(markets),
        MarketSnapshot.id.in_(referencing_evidence)
        | MarketSnapshot.id.in_(referencing_sessions)
        | MarketSnapshot.id.in_(referencing_tickets),
    )
    return c


def blockers_for(census: DependencyCensus) -> list[str]:
    """Which counted artifacts make the relabel unsafe.

    Derived from `census.rows()`, so a table added to the census is
    blocking-checked by construction rather than by remembering to add a
    second `if` down here. That omission is what made the first version
    incomplete.
    """

    return [
        f"{row.count} {row.label.strip()}" + (f" ({row.detail})" if row.detail else "")
        for row in census.rows()
        if row.blocking
    ]


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
            f"    (every table that can reach a game by FK: "
            f"{len(CENSUS_CLASSIFICATION)} classified)",
        ]
        for row in c.rows():
            out.append(row.render() + ("   <-- BLOCKS" if row.blocking else ""))
        out.append("")
        for status in sorted(c.checkpoints):
            out.append(
                f"    checkpoint runs {status:<10}          "
                f"{', '.join(sorted(c.checkpoints[status]))}"
            )
        if not c.checkpoints:
            out.append("    checkpoint runs                   none")
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
                "  Nothing has COMMITTED to the current week label. Every blocking",
                "  table above is zero -- no CAPTURED checkpoint, no evidence, no",
                "  forecast, no agent session by ANY route, no benchmark slot,",
                "  ticket, wager, pass decision, stake recommendation, settlement",
                "  or bankroll transaction.",
                "",
                f"  What remains is observation: prop markets, quotes, roster rows",
                f"  and {c.standalone_snapshots} standalone MarketSnapshot row(s). All of it hangs off",
                "  game_id / market_id -- neither of which this correction touches",
                "  -- so nothing is reinterpreted.",
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

    # The audit row is written first, but that ordering buys NOTHING
    # against failure and the earlier comment here claimed otherwise: both
    # statements are in one transaction, so if the UPDATE fails the INSERT
    # rolls back with it and no "a correction was attempted" record
    # survives. Durability of the pair is what the transaction guarantees,
    # not the order inside it.
    #
    # The order is kept for a smaller, real reason: the FK and the CHECK on
    # `game_scope_corrections` are evaluated at this flush, so a correction
    # that could not name its schedule call fails BEFORE `Game` is touched
    # at all, rather than after.
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


def main(argv: Sequence[str] | None = None, *, schedule_provider=None) -> int:
    """`schedule_provider` is a TEST SEAM only -- no CLI flag reaches it.

    The same seam `register_week_events` uses, for the same reason: without
    it a test of this entry point fetches the live nflverse release, which
    makes the suite slow, network-dependent, and quietly dependent on the
    real 2026 schedule agreeing with its fixtures. It cannot be used to
    substitute a schedule from the command line -- the season's frozen
    roster pin still chooses the implementation in production.
    """

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

    # A refusal is this tool's NORMAL, DESIGNED outcome -- a stale expected
    # week, an unresolvable fixture, a disputed kickoff, an unreachable
    # schedule, a no-op. Letting `RepairRefused` escape printed a traceback
    # for all of them, which reads like the tool broke rather than like the
    # tool worked. The guard firing is the success signal; it should look
    # like one, and the exit code carries the outcome for anything scripting
    # this.
    try:
        verdict = derive_authoritative_week(
            game_id=args.game_id, schedule_provider=schedule_provider,
        )

        with session_scope() as session:
            plan = plan_repair(
                session, verdict=verdict,
                expected_current_week=args.expect_current_week,
                expect_authoritative_week=args.expect_authoritative_week,
                reason=args.reason,
            )
            print(plan.render())
    except (RepairRefused, ScheduleSourceUnavailable) as exc:
        print("REFUSED — nothing written.")
        print()
        for line in textwrap.wrap(str(exc), 74):
            print(f"  {line}")
        return 1

    if not plan.safe:
        print()
        print("REFUSED — nothing written.")
        return 1
    if not args.apply:
        print()
        print("DRY RUN — no Game row written. Re-run with --apply to correct it.")
        return 0

    # Re-checked under the row lock in here, so it can still refuse even
    # though the plan above was safe: the dry run and this write are
    # separate transactions and the row may have moved between them.
    try:
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
    except (RepairRefused, ScheduleSourceUnavailable) as exc:
        print()
        print("REFUSED at write time — nothing written.")
        print()
        for line in textwrap.wrap(str(exc), 74):
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
