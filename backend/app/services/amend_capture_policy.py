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

from app.cli_args import number_list
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
    "benchmark_allocation_method",
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


def active_rules(session: Session, season_id: uuid.UUID, *, lock: bool = False) -> SeasonRules:
    """The one un-superseded rules row.

    `lock=True` takes a row lock for the amendment transaction. No network
    happens inside that transaction -- this is a pure database operation --
    so a short lock is appropriate here, unlike in the capture cycle where
    a lock would span a provider call.
    """

    stmt = (
        select(SeasonRules)
        .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
        .order_by(SeasonRules.effective_from.desc())
    )
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).scalars().first()
    if row is None:
        raise AmendmentRefused(f"season {season_id} has no active SeasonRules")
    return row


def _require_expected_parent(current: SeasonRules, expected: str | None) -> None:
    """The row the operator reviewed must be the row being superseded.

    Optional, but the CLI always supplies it: an amendment reviewed against
    one parent and applied against another is a silent methodology change.
    """

    if expected is not None and current.rules_version != expected:
        raise AmendmentRefused(
            f"expected the active rules to be {expected!r} but they are "
            f"{current.rules_version!r}. Another amendment landed since the dry "
            "run; re-read the diff before applying."
        )


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
    expected_current_version: str | None = None,
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
    _require_expected_parent(current, expected_current_version)

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
    expected_current_version: str | None = None,
) -> SeasonRules:
    """Clone-and-supersede, in one transaction.

    The active row is re-read UNDER A LOCK after validation, and its
    version re-checked, so two amendment commands racing cannot both
    supersede the same parent and leave two active clones. The dry run the
    operator read is therefore the row that actually gets superseded.
    """

    plan_amendment(
        session,
        season_id=season_id,
        max_observation_age_seconds=max_observation_age_seconds,
        refresh_retry_policy=refresh_retry_policy,
        rules_version=rules_version,
        amendment_reason=amendment_reason,
        effective_from=effective_from,
        expected_current_version=expected_current_version,
    )
    # Re-read under the lock: between the validation above and here another
    # amendment could have won, in which case the parent we validated is no
    # longer active and superseding it would leave two active rows.
    current = active_rules(session, season_id, lock=True)
    _require_expected_parent(current, expected_current_version)
    if current.rules_version == rules_version:
        raise AmendmentRefused(
            f"rules_version {rules_version!r} became the active version while this "
            "amendment was being prepared; another amendment won the race"
        )
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


def _backoff(raw: str) -> list[float]:
    return number_list(raw, field="--backoff-seconds")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze a season's capture policy via an append-only rules amendment"
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--max-observation-age-seconds", required=True, type=int)
    parser.add_argument("--max-attempts", required=True, type=int)
    parser.add_argument(
        "--backoff-seconds", required=True, type=_backoff,
        help='comma- or space-separated, e.g. 30,120. In PowerShell, quote it: '
             '"30,120" -- an unquoted comma there is array syntax.',
    )
    parser.add_argument("--window-guard-seconds", type=float, default=60.0)
    parser.add_argument("--request-budget-seconds", type=float, default=180.0)
    parser.add_argument("--rules-version", required=True, help="the NEW version")
    parser.add_argument(
        "--expect-current-version",
        help="the version the dry run showed as active. Supplied on --apply so the "
             "row you reviewed is the row that gets superseded.",
    )
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write the amendment. WITHOUT this flag the command is a "
             "dry run that prints the before/after diff and writes nothing.",
    )
    args = parser.parse_args(argv)

    retry = {
        "max_attempts": args.max_attempts,
        "backoff_seconds": args.backoff_seconds,
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
            expected_current_version=args.expect_current_version,
        )
        print(diff.render())
        observed_parent = diff.old_rules_version

    if not args.apply:
        print()
        print("DRY RUN — nothing written.")
        print("To commit, re-run with:")
        print(f"    --apply --expect-current-version {observed_parent}")
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
            expected_current_version=args.expect_current_version or observed_parent,
        )
        print()
        print(f"APPLIED. New active rules row: {clone.id} ({clone.rules_version})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
