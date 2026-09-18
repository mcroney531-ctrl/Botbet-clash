"""Freeze a season's capture policy through an APPEND-ONLY rules amendment.

`SeasonRules` is versioned, never updated in place (DATABASE.md §1). An
UPDATE would rewrite the rules a checkpoint was already captured under,
which is the one thing a frozen research contract exists to prevent: every
stored `AgentSession` and `MarketSnapshot` is only interpretable against
the rules that were in force when it was written.

So an amendment is: clone the active row, change ONLY the two
capture-policy fields plus the amendment bookkeeping, and point the old
row's `superseded_by` at the new one — all in one transaction.

This is a narrow command on purpose. It cannot change bankroll, Kelly
fraction, prop types, checkpoint windows, providers, the canonical
sportsbook, or any other methodology field; a guard verifies that after
building the clone, so a future edit to this module cannot widen it by
accident.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.forecast_lab.quote_selection import FreshnessConfigError, validate_max_observation_age
from app.marketdata.checkpoint_cycle import RefreshRetryPolicy

# Everything an amendment of THIS kind must leave untouched. Listed rather
# than inferred: a new methodology column added later should show up here
# deliberately, not be silently exempt from the guard.
METHODOLOGY_FIELDS = (
    "season_id",
    "starting_bankroll_cents",
    "canonical_sportsbook",
    "market_data_provider",
    "roster_data_provider",
    "research_settlement_provider",
    "research_settlement_delay_hours",
    "supported_prop_types",
    "devig_method",
    "benchmark_slate_size",
    "batch_methodology",
    "checkpoint_windows",
    "kelly_fraction",
    "standard_max_bankroll_fraction",
    "exceptional_max_bankroll_fraction",
    "minimum_stake_cents",
    "stake_increment_cents",
    "pounce_limit",
    "attribution_confidence_threshold",
    "weekly_decision_deadline_rule",
    "competition_requires_nonpushable_line",
)

POLICY_FIELDS = ("max_observation_age_seconds", "refresh_retry_policy")


class AmendmentRefused(RuntimeError):
    """The amendment was not applied. Nothing was written."""


@dataclass
class PolicyDiff:
    season_id: uuid.UUID
    season_name: str
    old_rules_version: str
    new_rules_version: str
    before: dict
    after: dict
    effective_from: datetime
    amendment_reason: str

    def render(self) -> str:
        out = [
            "=" * 72,
            "SEASON RULES AMENDMENT — capture policy",
            "=" * 72,
            f"  season        {self.season_name}  ({self.season_id})",
            f"  effective     {self.effective_from.isoformat()}",
            f"  reason        {self.amendment_reason}",
            "",
            f"  rules_version {self.old_rules_version}",
            f"             -> {self.new_rules_version}",
            "",
            "  --- changed ------------------------------------------------------",
        ]
        for field in POLICY_FIELDS:
            out.append(f"    {field}")
            out.append(f"      before  {self.before[field]!r}")
            out.append(f"      after   {self.after[field]!r}")
        out += [
            "",
            "  --- unchanged (guarded) ------------------------------------------",
            f"    {len(METHODOLOGY_FIELDS)} methodology fields verified identical",
            "",
            "  The old row is NOT modified except to set superseded_by. It stays",
            "  queryable, so every checkpoint already captured remains",
            "  interpretable against the rules that were in force for it.",
            "=" * 72,
        ]
        return "\n".join(out)


def active_rules(session: Session, season_id: uuid.UUID) -> SeasonRules:
    row = session.execute(
        select(SeasonRules)
        .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
        .order_by(SeasonRules.effective_from.desc())
    ).scalars().first()
    if row is None:
        raise AmendmentRefused(f"season {season_id} has no active SeasonRules")
    return row


def _snapshot(rules: SeasonRules) -> dict:
    return {field: getattr(rules, field) for field in POLICY_FIELDS}


def plan_amendment(
    session: Session,
    *,
    season_id: uuid.UUID,
    max_observation_age_seconds: int,
    refresh_retry_policy: dict,
    rules_version: str,
    amendment_reason: str,
    effective_from: datetime,
) -> PolicyDiff:
    """Build the diff WITHOUT writing anything.

    The retry policy is parsed through the same strict validator the cycle
    uses, so a malformed value is refused here rather than becoming a
    frozen rule that fails at capture time.
    """

    parsed = RefreshRetryPolicy.from_record(refresh_retry_policy)
    if parsed is None:
        raise AmendmentRefused("refresh_retry_policy must not be null in an amendment")
    # Through the canonical validator rather than a hand-rolled bounds
    # check: a second copy of "what counts as a usable tolerance" is how
    # the frozen rule and the runtime rule drift apart.
    try:
        validate_max_observation_age(max_observation_age_seconds)
    except FreshnessConfigError as exc:
        raise AmendmentRefused(str(exc)) from exc
    if max_observation_age_seconds is None:
        raise AmendmentRefused("max_observation_age_seconds must not be null in an amendment")

    current = active_rules(session, season_id)
    if current.rules_version == rules_version:
        raise AmendmentRefused(
            f"rules_version {rules_version!r} is already the active version; an "
            "amendment must introduce a new one"
        )

    season = session.get(Season, season_id)
    return PolicyDiff(
        season_id=season_id,
        season_name=season.name if season else "?",
        old_rules_version=current.rules_version,
        new_rules_version=rules_version,
        before=_snapshot(current),
        after={
            "max_observation_age_seconds": max_observation_age_seconds,
            "refresh_retry_policy": parsed.as_record(),
        },
        effective_from=effective_from,
        amendment_reason=amendment_reason,
    )


def apply_amendment(
    session: Session,
    *,
    season_id: uuid.UUID,
    max_observation_age_seconds: int,
    refresh_retry_policy: dict,
    rules_version: str,
    amendment_reason: str,
    effective_from: datetime,
) -> SeasonRules:
    """Clone-and-supersede, in one transaction."""

    plan_amendment(
        session,
        season_id=season_id,
        max_observation_age_seconds=max_observation_age_seconds,
        refresh_retry_policy=refresh_retry_policy,
        rules_version=rules_version,
        amendment_reason=amendment_reason,
        effective_from=effective_from,
    )
    current = active_rules(session, season_id)
    parsed = RefreshRetryPolicy.from_record(refresh_retry_policy)

    clone = SeasonRules(
        **{field: getattr(current, field) for field in METHODOLOGY_FIELDS},
        rules_version=rules_version,
        max_observation_age_seconds=max_observation_age_seconds,
        refresh_retry_policy=parsed.as_record(),
        amendment_reason=amendment_reason,
        effective_from=effective_from,
    )
    session.add(clone)
    session.flush()

    # Verified AFTER building, so a future edit that widened the clone
    # would fail here rather than silently rewrite methodology.
    drifted = [f for f in METHODOLOGY_FIELDS if getattr(clone, f) != getattr(current, f)]
    if drifted:
        raise AmendmentRefused(
            f"the amendment would change methodology fields {drifted}; a capture-policy "
            "amendment may only touch " + ", ".join(POLICY_FIELDS)
        )

    current.superseded_by = clone.id
    session.flush()
    return clone


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze a season's capture policy via an append-only rules amendment"
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--max-observation-age-seconds", required=True, type=int)
    parser.add_argument("--max-attempts", required=True, type=int)
    parser.add_argument(
        "--backoff-seconds", required=True,
        help="comma-separated, e.g. 30,120",
    )
    parser.add_argument("--window-guard-seconds", type=float, default=60.0)
    parser.add_argument("--request-budget-seconds", type=float, default=180.0)
    parser.add_argument("--rules-version", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write the amendment. WITHOUT this flag the command is a "
             "dry run that prints the before/after diff and writes nothing.",
    )
    args = parser.parse_args(argv)

    retry = {
        "max_attempts": args.max_attempts,
        "backoff_seconds": [float(x) for x in args.backoff_seconds.split(",") if x.strip()],
        "window_guard_seconds": args.window_guard_seconds,
        "request_budget_seconds": args.request_budget_seconds,
    }
    effective_from = datetime.now(timezone.utc)

    with session_scope() as session:
        diff = plan_amendment(
            session,
            season_id=args.season_id,
            max_observation_age_seconds=args.max_observation_age_seconds,
            refresh_retry_policy=retry,
            rules_version=args.rules_version,
            amendment_reason=args.reason,
            effective_from=effective_from,
        )
        print(diff.render())

    if not args.apply:
        print()
        print("DRY RUN — nothing written. Re-run with --apply to commit this amendment.")
        return 0

    with session_scope() as session:
        clone = apply_amendment(
            session,
            season_id=args.season_id,
            max_observation_age_seconds=args.max_observation_age_seconds,
            refresh_retry_policy=retry,
            rules_version=args.rules_version,
            amendment_reason=args.reason,
            effective_from=effective_from,
        )
        print()
        print(f"APPLIED. New active rules row: {clone.id} ({clone.rules_version})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
