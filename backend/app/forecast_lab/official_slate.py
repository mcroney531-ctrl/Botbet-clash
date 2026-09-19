"""The ONE production entry point for committing a benchmark slate.

An operator supplies a season and a week. Nothing else. Every
methodological value — slate size, prop types, allocation method,
checkpoint windows, schedule provider — is resolved from the season's
frozen `SeasonRules`, because a methodology an operator can hand in at the
command line is not frozen in any useful sense. `commit_from_planned_
fixtures` still accepts those values (the Phase-2B synthetic adapter needs
them), which is exactly why this layer exists above it.

**The deadline is checked AFTER the schedule has been fetched.** A command
that read the clock at startup could begin thirty seconds before the
deadline, spend forty seconds fetching and parsing, and commit past the
methodology deadline while truthfully reporting it started in time. The
clock that matters is the one at the moment of decision, so it is read
after the network work is done and before the transaction opens.

Choreography, in this order and for these reasons:

    1. fetch the authoritative schedule          (network, no transaction)
    2. persist the provider call                 (success or failure)
    3. build and validate the complete pool      (fails on a missing kickoff)
    4. read the clock                            (AFTER the fetch)
    5. compute the earliest OPENING start        (frozen windows)
    6. refuse if the deadline has passed         (no override exists)
    7. commit in one short transaction

Fails closed on a NULL allocation method: no methodology frozen means no
official slate, not a silent inheritance of the V0 allocator.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from app.db.models.forecast_lab import BenchmarkSlatePlan
from app.db.models.season import Week
from app.db.session import session_scope
from app.forecast_lab.benchmark_slate_service import (
    PlanProvenance,
    commit_from_planned_fixtures,
)
from app.forecast_lab.checkpoint_window import compute_window
from app.forecast_lab.fixture_identity import (
    FIXTURE_KEY_VERSION,
    PLANNING_INPUT_VERSION,
    IncompletePool,
    PlannedFixture,
    planned_pool,
    pool_fingerprint,
)
from app.forecast_lab.slate_allocation import (
    ALLOCATION_METHODS,
    APPROVED_METHODS,
    UnknownAllocationMethod,
    allocate,
)
from app.marketdata.game_registration import (
    ScheduleSourceUnavailable,
    _schedule_provider_for,
    _season_pins,
)
from app.marketdata.telemetry import finish_run, record_call, start_run
from app.services.amend_capture_policy import AmendmentRefused, active_rules
from app.marketdata.week_resolution import RESOLVER_VERSION

OPENING = "OPENING"


class SlateCommitRefused(RuntimeError):
    """Nothing was committed."""


class MethodologyNotFrozen(SlateCommitRefused):
    """The season has not frozen a value this commitment requires."""


@dataclass
class SlatePlanProposal:
    season_id: uuid.UUID
    week_number: int
    season_name: str | None = None
    rules_version: str | None = None
    schedule_provider: str | None = None
    schedule_provider_call_id: uuid.UUID | None = None
    allocation_method: str | None = None
    slate_size: int | None = None
    prop_types: list[str] = field(default_factory=list)
    pool: tuple[PlannedFixture, ...] = ()
    chosen: tuple[PlannedFixture, ...] = ()
    earliest_opening_at: datetime | None = None
    decided_at: datetime | None = None
    committed: bool = False
    # When the proposal was DECIDED (after the fetch) versus when the write
    # actually happened. Both are kept because the deadline is checked twice
    # and an auditor should be able to see both moments.
    committed_at: datetime | None = None
    plan_id: uuid.UUID | None = None

    @property
    def fingerprint(self) -> str:
        return pool_fingerprint(self.pool)

    @property
    def seconds_to_deadline(self) -> float | None:
        if self.earliest_opening_at is None or self.decided_at is None:
            return None
        return (self.earliest_opening_at - self.decided_at).total_seconds()

    @property
    def past_deadline(self) -> bool:
        remaining = self.seconds_to_deadline
        return remaining is not None and remaining <= 0

    def render(self) -> str:
        out = [
            "=" * 78,
            f"BENCHMARK SLATE {'COMMITTED' if self.committed else 'PREVIEW'} "
            f"— week {self.week_number}",
            "=" * 78,
            f"  season            {self.season_name}  ({self.season_id})",
            f"  rules version     {self.rules_version}",
            f"  schedule          {self.schedule_provider}  "
            f"(call {self.schedule_provider_call_id})",
            f"  allocation        {self.allocation_method}  (FROZEN in SeasonRules)",
            f"  slate size        {self.slate_size}",
            f"  prop types        {', '.join(self.prop_types)}",
            f"  fixture key       {FIXTURE_KEY_VERSION}",
            f"  planning input    {PLANNING_INPUT_VERSION}",
            "",
            "  --- the complete fixture pool the allocator saw ----------------",
            f"    {len(self.pool)} fixture(s), fingerprint {self.fingerprint[:16]}…",
        ]
        chosen_keys = {f.key.value for f in self.chosen}
        for fixture in sorted(self.pool, key=lambda f: (f.kickoff_at, f.key.value)):
            mark = "  <-- SLOT" if fixture.key.value in chosen_keys else ""
            out.append(
                f"      {fixture.key.value:24} {fixture.kickoff_at.isoformat()}{mark}"
            )
        # "proposed" is a claim about what has NOT happened yet. Once the
        # plan is committed these rows are the frozen slate, and calling
        # them proposed invites someone to think they are still negotiable.
        out += [
            "",
            (
                "  --- committed slots --------------------------------------------"
                if self.committed
                else "  --- proposed slots ---------------------------------------------"
            ),
        ]
        for i, fixture in enumerate(self.chosen, start=1):
            stat = self.prop_types[(i - 1) % len(self.prop_types)] if self.prop_types else "?"
            out.append(f"    slot {i}  {fixture.key.value:24} target {stat}")
        out += [
            "",
            "  --- the deadline -----------------------------------------------",
            f"    earliest OPENING start   {self.earliest_opening_at.isoformat()}"
            if self.earliest_opening_at else "    earliest OPENING start   unknown",
            f"    decided at               {self.decided_at.isoformat()}"
            if self.decided_at else "    decided at               —",
        ]
        remaining = self.seconds_to_deadline
        if remaining is not None:
            out.append(
                f"    margin                   {remaining / 3600:+.2f}h"
                + ("   PAST DEADLINE" if remaining <= 0 else "")
            )
        out += [
            "",
            "    The clock is read AFTER the schedule fetch, so this margin is",
            "    the real one at the moment of decision — not the one the",
            "    command started with.",
            "=" * 78,
        ]
        return "\n".join(out)


def propose_slate(
    *,
    season_id: uuid.UUID,
    week_number: int,
    schedule_provider=None,
    now: datetime | None = None,
) -> SlatePlanProposal:
    """Everything up to, but not including, the write."""

    if week_number < 1:
        raise SlateCommitRefused(f"--week-number must be a real NFL week, got {week_number}")

    proposal = SlatePlanProposal(season_id=season_id, week_number=week_number)

    with session_scope() as session:
        season, rules = _season_pins(session, season_id)
        proposal.season_name = season.name
        proposal.rules_version = rules.rules_version
        proposal.allocation_method = rules.benchmark_allocation_method
        proposal.slate_size = rules.benchmark_slate_size
        proposal.prop_types = list(rules.supported_prop_types or [])
        windows = dict(rules.checkpoint_windows or {})
        season_year = season.year
        roster_pin = rules.roster_data_provider

    # Fail closed BEFORE spending a fetch: a NULL allocation method means no
    # methodology has been reviewed, and inheriting V0 is precisely the
    # accident this field exists to prevent.
    if proposal.allocation_method is None:
        raise MethodologyNotFrozen(
            f"season {season_id} (rules {proposal.rules_version}) has not frozen "
            "benchmark_allocation_method. NULL is not a default and not "
            "permission to inherit ROUND_ROBIN_BY_KICKOFF_V0 — that allocator "
            "takes the first N fixtures by kickoff and was never reviewed as "
            "methodology. Freeze one with a rules amendment first. Reviewed "
            "methods: " + ", ".join(sorted(ALLOCATION_METHODS))
        )
    if proposal.allocation_method not in ALLOCATION_METHODS:
        raise UnknownAllocationMethod(
            f"season {season_id} is pinned to allocation method "
            f"{proposal.allocation_method!r}, which no reviewed allocator "
            "implements. Known: " + ", ".join(sorted(ALLOCATION_METHODS))
        )
    # Implemented is not approved. Every other method in the registry was
    # written to be MEASURED against the approved one, and two of them have
    # named defects -- one of which structurally excludes Monday night.
    if proposal.allocation_method not in APPROVED_METHODS:
        method = ALLOCATION_METHODS[proposal.allocation_method]
        raise MethodologyNotFrozen(
            f"season {season_id} is pinned to {proposal.allocation_method}, which "
            f"is implemented but NOT APPROVED: {method.defect} Approved: "
            + ", ".join(sorted(APPROVED_METHODS))
        )
    if not proposal.prop_types:
        raise MethodologyNotFrozen(f"season {season_id} has no supported_prop_types")
    if OPENING not in windows:
        raise MethodologyNotFrozen(
            f"season {season_id} has no OPENING checkpoint window frozen; the "
            "commit deadline cannot be computed without it"
        )

    # 1-2. Network first, outside every transaction; call persisted either way.
    source = schedule_provider or _schedule_provider_for(roster_pin)
    proposal.schedule_provider = getattr(source, "provider_name", "?")
    result = source.fetch_schedule(season=season_year)

    with session_scope() as session:
        run = start_run(
            session, provider=proposal.schedule_provider, operation="FETCH_SCHEDULE",
            season_id=season_id, week_number=week_number,
        )
        call = record_call(
            session, run=run, metadata=result.call_metadata, success=result.ok,
            error_category=result.error.category if result.error else None,
            error_message=result.error.message if result.error else None,
        )
        proposal.schedule_provider_call_id = call.id
        finish_run(session, run=run, status="SUCCEEDED" if result.ok else "FAILED")

    if not result.ok:
        raise SlateCommitRefused(
            f"schedule unavailable ({result.error.category}: {result.error.message}). "
            "A benchmark pool must come from the authoritative schedule; there is "
            "no fallback."
        )

    # 3. The COMPLETE pool. A missing kickoff is fatal, not skipped.
    #
    # Re-raised as a commit refusal so a caller needs one exception type for
    # "nothing was committed" rather than having to know which layer noticed.
    try:
        proposal.pool = planned_pool(
            result.payload, week=week_number, require_kickoffs=True,
        )
    except IncompletePool as exc:
        raise SlateCommitRefused(str(exc)) from exc
    if not proposal.pool:
        raise SlateCommitRefused(
            f"the {proposal.schedule_provider} schedule has no week-{week_number} "
            f"regular-season fixtures for {season_year}"
        )

    # 4-5. The clock, read now that the slow part is done, and the deadline.
    proposal.decided_at = now or datetime.now(timezone.utc)
    proposal.earliest_opening_at = min(
        compute_window(f.kickoff_at, OPENING, windows).window_start for f in proposal.pool
    )
    proposal.chosen = allocate(
        proposal.pool, slots=proposal.slate_size, method=proposal.allocation_method
    )
    return proposal


def commit_official_slate(
    *,
    season_id: uuid.UUID,
    week_number: int,
    schedule_provider=None,
    now: datetime | None = None,
) -> SlatePlanProposal:
    """Propose, enforce the deadline, then write. No override exists."""

    proposal = propose_slate(
        season_id=season_id, week_number=week_number,
        schedule_provider=schedule_provider, now=now,
    )

    # 6. The deadline. Deliberately has no bypass flag: a methodology
    # deadline an operator can wave through is not a deadline.
    if proposal.past_deadline:
        raise SlateCommitRefused(
            f"week {week_number}'s earliest OPENING window opened at "
            f"{proposal.earliest_opening_at.isoformat()} and it is now "
            f"{proposal.decided_at.isoformat()} "
            f"({-proposal.seconds_to_deadline / 3600:.2f}h late). A benchmark "
            "slate is precommitted or it is not a benchmark; committing after a "
            "capture window has opened means the sample could have been chosen "
            "knowing what the market did. There is no override."
        )

    # 7. One short transaction. NOTHING observed before it is trusted.
    #
    # The proposal was built across a network call, so between step 1 and
    # here: an allocation-method amendment could have landed, leaving the
    # slate committed under a superseded rules_version; the clock could have
    # crossed the deadline the pre-check just passed; and another committer
    # could have taken this week. All three are re-checked under locks, and
    # no network happens inside them.
    with session_scope() as session:
        # The WEEK first, so two simultaneous committers serialize here and
        # the loser refuses cleanly instead of colliding with the UNIQUE
        # index and surfacing an IntegrityError.
        week = session.execute(
            select(Week).where(
                Week.season_id == season_id, Week.week_number == week_number
            ).with_for_update()
        ).scalar_one_or_none()
        if week is None:
            raise SlateCommitRefused(
                f"season {season_id} has no Week row for week {week_number}; a plan "
                "is keyed by week and cannot be committed without one"
            )

        # The RULES. The ACTIVE row is resolved UNDER THE LOCK, in one
        # statement -- not resolved unlocked and then locked by id.
        #
        # The earlier version did exactly that, and it proved nothing: if an
        # amendment landed between the unlocked read and the lock, the row
        # it locked was the superseded PARENT, whose rules_version still
        # matched the proposal. It would have locked a dead row, compared
        # a stale version to itself, and committed. The structural test
        # showed a FOR UPDATE existed; it could not show WHICH row.
        #
        # `active_rules(..., lock=True)` selects on `superseded_by IS NULL`
        # with the lock attached, so whatever it returns is active at the
        # write boundary by construction.
        try:
            live = active_rules(session, season_id, lock=True)
        except AmendmentRefused as exc:
            raise SlateCommitRefused(
                f"the season's active rules could not be resolved at the write "
                f"boundary ({exc}). A concurrent amendment may be in flight; "
                "nothing committed. Re-run the preview."
            ) from exc
        if live.rules_version != proposal.rules_version:
            raise SlateCommitRefused(
                f"the season's methodology changed while this slate was being "
                f"prepared: proposed under rules {proposal.rules_version}, the "
                f"active rules are now {live.rules_version}. Nothing committed — "
                "re-run the preview against the current rules."
            )
        if live.benchmark_allocation_method != proposal.allocation_method:
            raise SlateCommitRefused(
                f"the frozen allocation method changed while this slate was being "
                f"prepared: proposed {proposal.allocation_method}, the active rules "
                f"now say {live.benchmark_allocation_method}. Nothing committed."
            )

        # The DEADLINE, on the clock at the write boundary. The post-fetch
        # check is an early refusal; this one is the guarantee.
        write_now = now or datetime.now(timezone.utc)
        if write_now >= proposal.earliest_opening_at:
            raise SlateCommitRefused(
                f"the deadline passed between proposal and write: week "
                f"{week_number}'s earliest OPENING window opened at "
                f"{proposal.earliest_opening_at.isoformat()} and the write began "
                f"at {write_now.isoformat()}. Nothing committed."
            )

        existing = session.execute(
            select(BenchmarkSlatePlan).where(BenchmarkSlatePlan.week_id == week.id)
        ).scalar_one_or_none()
        if existing is not None:
            raise SlateCommitRefused(
                f"week {week_number} already has benchmark slate plan {existing.id} "
                f"committed at {existing.committed_at.isoformat()}. A plan is "
                "committed once per week and is never recommitted."
            )
        plan = commit_from_planned_fixtures(
            session,
            week_id=week.id,
            fixtures=proposal.pool,
            target_slot_count=proposal.slate_size,
            prop_types=proposal.prop_types,
            allocation_method=proposal.allocation_method,
            # The WRITE-BOUNDARY clock, not `proposal.decided_at`. The
            # deadline is enforced against `write_now`, so calling the
            # earlier proposal time "committed" would put a timestamp in the
            # durable record that no write ever happened at.
            committed_at=write_now,
            provenance=PlanProvenance(
                rules_version=proposal.rules_version,
                schedule_provider_call_id=proposal.schedule_provider_call_id,
                resolver_version=RESOLVER_VERSION,
                earliest_opening_at=proposal.earliest_opening_at,
            ),
        )
        proposal.plan_id = plan.id
        proposal.committed_at = write_now
        proposal.committed = True
    return proposal


def main(
    argv: Sequence[str] | None = None, *, schedule_provider=None,
    now: datetime | None = None,
) -> int:
    """`now` is a TEST SEAM only -- no CLI flag reaches it.

    A deadline an operator can move is not a deadline. But a test that
    calls this on the real clock passes or fails by the calendar: the
    fixture week's OPENING window opened on a fixed date, so the same test
    was green one day and PAST DEADLINE the next. The seam makes the test
    about the code rather than about today.
    """

    parser = argparse.ArgumentParser(
        description="Commit a week's benchmark slate from the authoritative "
                    "schedule. Methodology comes from frozen SeasonRules only.",
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--week-number", required=True, type=int)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually commit the plan. WITHOUT this flag nothing is written "
             "except the schedule provider-call audit telemetry.",
    )
    args = parser.parse_args(argv)

    try:
        if args.apply:
            proposal = commit_official_slate(
                season_id=args.season_id, week_number=args.week_number,
                schedule_provider=schedule_provider, now=now,
            )
        else:
            proposal = propose_slate(
                season_id=args.season_id, week_number=args.week_number,
                schedule_provider=schedule_provider, now=now,
            )
    except (SlateCommitRefused, UnknownAllocationMethod, IncompletePool,
            ScheduleSourceUnavailable, LookupError) as exc:
        print("REFUSED — nothing committed.")
        print()
        print(f"  {exc}")
        return 1

    print(proposal.render())
    if proposal.committed:
        print()
        print(f"COMMITTED. Plan {proposal.plan_id}: {len(proposal.chosen)} slot(s) "
              f"over a {len(proposal.pool)}-fixture pool "
              f"(fingerprint {proposal.fingerprint}).")
        return 0
    if proposal.past_deadline:
        print()
        print("PAST DEADLINE — --apply would refuse. The earliest OPENING window "
              "has already opened.")
        return 1
    print()
    print("PREVIEW — nothing committed; provider audit telemetry recorded. "
          "Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
