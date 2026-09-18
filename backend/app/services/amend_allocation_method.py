"""Freeze the benchmark allocation methodology. Append-only, narrow.

A DEDICATED command rather than a widened `amend_capture_policy`. That
tool exists to change the freshness/retry policy and guards twenty-three
methodology fields as untouchable; teaching it a second mutable field
would make its own guard weaker every time a third kind of amendment came
along, and "which fields may this command change" is the whole safety
property. So this command may change exactly one column, and every field
the other command guards — plus the two it may change — is guarded here.

The value being frozen is a REVIEWED methodology choice, so the command
refuses any allocator that is merely implemented. Three of the four
methods in the registry were written to be measured against the approved
one, and two carry recorded defects: `ROUND_ROBIN_BY_KICKOFF_V0` takes the
first N fixtures by kickoff, and `KICKOFF_BLOCK_STRATIFIED_V1`
structurally excludes the latest kickoff block.

Dry run by default. `--apply` required.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy.orm import Session

from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.forecast_lab.slate_allocation import ALLOCATION_METHODS, APPROVED_METHODS
from app.services.amend_capture_policy import (
    METHODOLOGY_FIELDS,
    POLICY_FIELDS,
    AmendmentRefused,
    _require_expected_parent,
    active_rules,
)

FIELD = "benchmark_allocation_method"

# Everything this amendment must leave identical: the fields the capture
# amendment guards, MINUS the one column this command exists to change,
# PLUS the two columns that command may change. Derived rather than
# retyped, so a methodology field added to either list is guarded here
# without anyone remembering to copy it.
GUARDED_FIELDS = tuple(f for f in METHODOLOGY_FIELDS if f != FIELD) + POLICY_FIELDS


@dataclass
class AllocationDiff:
    season_id: uuid.UUID
    season_name: str
    old_rules_version: str
    new_rules_version: str
    before: str | None
    after: str
    effective_from: datetime
    applied: bool = False
    new_rules_id: uuid.UUID | None = None

    def render(self) -> str:
        method = ALLOCATION_METHODS[self.after]
        out = [
            "=" * 72,
            f"SEASON RULES AMENDMENT — {FIELD}"
            + ("  (APPLIED)" if self.applied else "  (DRY RUN)"),
            "=" * 72,
            f"  season          {self.season_name}  ({self.season_id})",
            f"  lineage         {self.old_rules_version}  ->  {self.new_rules_version}",
            f"  effective from  {self.effective_from.isoformat()}",
            "",
            "  --- the one field this command may change ------------------------",
            f"    {FIELD}",
            f"      before  {self.before!r}",
            f"      after   {self.after!r}",
            "",
            f"    {method.summary}",
            "",
            "  --- unchanged (guarded) ------------------------------------------",
            f"    {len(GUARDED_FIELDS)} fields verified identical, including the "
            "freshness",
            "    and retry policy this command may not touch.",
            "",
            "  --- not approved, and why ----------------------------------------",
        ]
        for name in sorted(set(ALLOCATION_METHODS) - APPROVED_METHODS):
            out.append(f"    {name}")
            out.append(f"      {ALLOCATION_METHODS[name].defect}")
        out += [
            "",
            "  The old row is NOT modified except to set superseded_by. It stays",
            "  queryable, so any slate already committed remains interpretable",
            "  against the rules that were in force for it.",
            "=" * 72,
        ]
        return "\n".join(out)


def _verify_guarded(old: SeasonRules, new: SeasonRules) -> None:
    """Every field but the one. Checked on the CLONE, after it is built.

    Comparing the inputs would prove the intent; comparing the rows proves
    the outcome, which is the thing an audit later needs.
    """

    drifted = [
        f"{field}: {getattr(old, field)!r} -> {getattr(new, field)!r}"
        for field in GUARDED_FIELDS
        if getattr(old, field) != getattr(new, field)
    ]
    if drifted:
        raise AmendmentRefused(
            "this amendment may change only "
            f"{FIELD}, but the new row also differs in: " + "; ".join(drifted)
        )


def plan_allocation_amendment(
    session: Session,
    *,
    season_id: uuid.UUID,
    allocation_method: str,
    new_rules_version: str,
    effective_from: datetime | None = None,
    expected_current_version: str | None = None,
) -> AllocationDiff:
    """Build the diff. Writes nothing."""

    if allocation_method not in ALLOCATION_METHODS:
        raise AmendmentRefused(
            f"{allocation_method!r} is not an implemented allocator. Known: "
            + ", ".join(sorted(ALLOCATION_METHODS))
        )
    if allocation_method not in APPROVED_METHODS:
        raise AmendmentRefused(
            f"{allocation_method!r} is implemented but NOT APPROVED: "
            f"{ALLOCATION_METHODS[allocation_method].defect} "
            "Approved: " + ", ".join(sorted(APPROVED_METHODS))
        )

    season = session.get(Season, season_id)
    if season is None:
        raise AmendmentRefused(f"season {season_id} not found")
    current = active_rules(session, season_id)
    _require_expected_parent(current, expected_current_version)

    if current.rules_version == new_rules_version:
        raise AmendmentRefused(
            f"the active rules are already {new_rules_version!r}; an amendment "
            "appends a new version rather than rewriting one"
        )
    if getattr(current, FIELD) == allocation_method:
        raise AmendmentRefused(
            f"the active rules already freeze {FIELD} as {allocation_method!r}; "
            "nothing to amend"
        )

    return AllocationDiff(
        season_id=season_id,
        season_name=season.name,
        old_rules_version=current.rules_version,
        new_rules_version=new_rules_version,
        before=getattr(current, FIELD),
        after=allocation_method,
        effective_from=effective_from or datetime.now(timezone.utc),
    )


def apply_allocation_amendment(
    session: Session,
    *,
    season_id: uuid.UUID,
    allocation_method: str,
    new_rules_version: str,
    effective_from: datetime | None = None,
    expected_current_version: str | None = None,
) -> AllocationDiff:
    """Clone, change one column, supersede the parent. One transaction.

    The parent is taken `FOR UPDATE` and re-validated inside it: the dry
    run and the apply are separate processes, and an amendment that landed
    between them would otherwise be silently superseded by this one.
    """

    diff = plan_allocation_amendment(
        session, season_id=season_id, allocation_method=allocation_method,
        new_rules_version=new_rules_version, effective_from=effective_from,
        expected_current_version=expected_current_version,
    )

    current = active_rules(session, season_id, lock=True)
    _require_expected_parent(current, expected_current_version)
    if current.rules_version != diff.old_rules_version:
        raise AmendmentRefused(
            f"the active rules moved while this amendment was being prepared: "
            f"planned against {diff.old_rules_version!r}, found "
            f"{current.rules_version!r}. Nothing written."
        )

    clone = SeasonRules(
        **{
            field: getattr(current, field)
            for field in (*METHODOLOGY_FIELDS, *POLICY_FIELDS)
        }
    )
    setattr(clone, FIELD, allocation_method)
    clone.rules_version = new_rules_version
    clone.effective_from = diff.effective_from
    session.add(clone)
    session.flush()

    _verify_guarded(current, clone)

    current.superseded_by = clone.id
    session.flush()

    diff.applied = True
    diff.new_rules_id = clone.id
    return diff


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Append-only amendment of SeasonRules.{FIELD}. This command "
                    "may change that one column and nothing else.",
    )
    parser.add_argument("--season-id", required=True, type=uuid.UUID)
    parser.add_argument("--allocation-method", required=True)
    parser.add_argument("--new-rules-version", required=True)
    parser.add_argument(
        "--expect-current-version", required=True,
        help="the rules version the dry run showed. The amendment refuses if "
             "the active row says anything else.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually append the new rules row. WITHOUT this flag nothing is "
             "written.",
    )
    args = parser.parse_args(argv)

    try:
        with session_scope() as session:
            diff = plan_allocation_amendment(
                session, season_id=args.season_id,
                allocation_method=args.allocation_method,
                new_rules_version=args.new_rules_version,
                expected_current_version=args.expect_current_version,
            )
            print(diff.render())
    except AmendmentRefused as exc:
        print("REFUSED — nothing written.")
        print()
        print(f"  {exc}")
        return 1

    if not args.apply:
        print()
        print("DRY RUN — nothing written. Re-run with --apply to append it.")
        return 0

    try:
        with session_scope() as session:
            diff = apply_allocation_amendment(
                session, season_id=args.season_id,
                allocation_method=args.allocation_method,
                new_rules_version=args.new_rules_version,
                expected_current_version=args.expect_current_version,
            )
            print(diff.render())
    except AmendmentRefused as exc:
        print()
        print("REFUSED at write time — nothing written.")
        print()
        print(f"  {exc}")
        return 1

    print()
    print(f"APPLIED. New active rules row {diff.new_rules_id} "
          f"({diff.new_rules_version}): {FIELD} {diff.before!r} -> {diff.after!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
