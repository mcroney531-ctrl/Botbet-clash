"""Explicit, audited correction of a game's `week_number`.

Silent repair is forbidden. Audited correction is not — and leaving a
known-false week in the durable research record forever is not better
integrity than correcting it in the open. It is just a different way to be
wrong, with no paper trail either way.

This exists for one known case. The Phase 4A.2 acceptance run assigned
DET @ BUF week 3 by hand, before authoritative schedule verification
existed; the nflverse 2026 schedule resolves that fixture to week 2. The
event identity is correct; one attribute is not.

What a correction may NEVER touch:

    Game.id          permanent
    external_ref     permanent provider identity
    home/away teams  identity
    kickoff_at       drives the checkpoint windows
    PropMarket ids   observations hang off them
    PropQuote ids    immutable observations

It refuses outright if the game carries any CAPTURED checkpoint or other
competitive artifact whose interpretation would change under a week
correction. At that point this is no longer a mislabelled attribute; it is
a dependency graph, and a human needs to see it.

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

from app.db.models.forecast_lab import EvidenceSnapshot, ForecastObservation
from app.db.models.markets import (
    CheckpointRun,
    Game,
    GameScopeCorrection,
    MarketSnapshot,
    PropMarket,
)
from app.db.session import session_scope
from app.marketdata.week_resolution import RESOLVER_VERSION

DEFAULT_REASON = (
    "Phase 4A.2 acceptance game was manually assigned its week before "
    "authoritative schedule verification existed; the nflverse schedule "
    "resolves this fixture to a different week."
)


class RepairRefused(RuntimeError):
    """Nothing was written."""


@dataclass
class RepairPlan:
    game_id: uuid.UUID
    external_ref: str
    away: str
    home: str
    kickoff_at: datetime
    current_week: int
    authoritative_week: int
    reason: str
    blockers: list[str] = field(default_factory=list)
    markets: int = 0
    checkpoints: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.blockers

    def render(self) -> str:
        out = [
            "=" * 74,
            "GAME SCOPE CORRECTION — week_number",
            "=" * 74,
            f"  game_id       {self.game_id}",
            f"  external_ref  {self.external_ref}   (UNCHANGED)",
            f"  fixture       {self.away} @ {self.home}   (UNCHANGED)",
            f"  kickoff_at    {self.kickoff_at.isoformat()}   (UNCHANGED)",
            "",
            f"  week_number   {self.current_week}  ->  {self.authoritative_week}",
            f"  reason        {self.reason}",
            "",
            "  --- dependency check -------------------------------------------",
            f"    prop markets on this game     {self.markets}",
            f"    checkpoint runs               {', '.join(self.checkpoints) or 'none'}",
        ]
        if self.blockers:
            out += ["", "  REFUSED — correcting the week would reinterpret:"]
            out += [f"    {b}" for b in self.blockers]
            out += [
                "",
                "  This is no longer a mislabelled attribute; it is a dependency",
                "  graph. Bring it back for review rather than applying a simple",
                "  repair.",
            ]
        else:
            out += [
                "",
                "  No CAPTURED checkpoint or competitive artifact depends on the",
                "  week label. PropMarket and PropQuote rows hang off game_id,",
                "  which does not change, so no observation is reinterpreted.",
            ]
        out.append("=" * 74)
        return "\n".join(out)


def plan_repair(
    session: Session,
    *,
    game_id: uuid.UUID,
    expected_current_week: int,
    authoritative_week: int,
    reason: str = DEFAULT_REASON,
) -> RepairPlan:
    """Build the plan and run the dependency check. Writes nothing."""

    game = session.get(Game, game_id)
    if game is None:
        raise RepairRefused(f"game {game_id} not found")

    # The operator must be repairing the row they reviewed.
    if game.week_number != expected_current_week:
        raise RepairRefused(
            f"expected week_number {expected_current_week} but the row says "
            f"{game.week_number}. Re-read the inspector before applying."
        )
    if game.week_number == authoritative_week:
        raise RepairRefused(
            f"game {game_id} is already week {authoritative_week}; nothing to correct"
        )

    plan = RepairPlan(
        game_id=game_id,
        external_ref=game.external_ref,
        away=game.away_team_canonical,
        home=game.home_team_canonical,
        kickoff_at=game.kickoff_at,
        current_week=game.week_number,
        authoritative_week=authoritative_week,
        reason=reason,
    )

    plan.markets = session.execute(
        select(func.count()).select_from(PropMarket).where(PropMarket.game_id == game_id)
    ).scalar()

    runs = session.execute(
        select(CheckpointRun).where(CheckpointRun.game_id == game_id)
    ).scalars().all()
    plan.checkpoints = [f"{r.checkpoint_type}={r.status}" for r in runs]

    captured = [r for r in runs if r.status == "CAPTURED"]
    if captured:
        plan.blockers.append(
            f"{len(captured)} CAPTURED checkpoint run(s): "
            + ", ".join(r.checkpoint_type for r in captured)
        )

    # Anything built ON a capture is competitive state, not raw observation.
    for model, label in (
        (EvidenceSnapshot, "EvidenceSnapshot"),
        (ForecastObservation, "ForecastObservation"),
    ):
        count = session.execute(
            select(func.count()).select_from(model)
            .join(PropMarket, PropMarket.id == model.market_id)
            .where(PropMarket.game_id == game_id)
        ).scalar()
        if count:
            plan.blockers.append(f"{count} {label} row(s)")

    snapshots = session.execute(
        select(func.count()).select_from(MarketSnapshot)
        .join(PropMarket, PropMarket.id == MarketSnapshot.market_id)
        .where(PropMarket.game_id == game_id)
    ).scalar()
    if snapshots:
        plan.blockers.append(f"{snapshots} MarketSnapshot row(s)")

    return plan


def apply_repair(
    session: Session,
    *,
    game_id: uuid.UUID,
    expected_current_week: int,
    authoritative_week: int,
    reason: str = DEFAULT_REASON,
    schedule_provider_call_id: uuid.UUID | None = None,
) -> GameScopeCorrection:
    """Append the audit row, then correct the field. One transaction."""

    plan = plan_repair(
        session, game_id=game_id, expected_current_week=expected_current_week,
        authoritative_week=authoritative_week, reason=reason,
    )
    if not plan.safe:
        raise RepairRefused(
            "correcting the week would reinterpret existing artifacts: "
            + "; ".join(plan.blockers)
        )

    game = session.get(Game, game_id)
    # The audit row is written FIRST and is append-only. If the update
    # failed after it, the record would still say a correction was
    # attempted; if the order were reversed and the audit insert failed,
    # the field would have moved with nothing explaining why.
    correction = GameScopeCorrection(
        game_id=game_id,
        field_corrected="week_number",
        old_value=str(game.week_number),
        new_value=str(authoritative_week),
        schedule_provider_call_id=schedule_provider_call_id,
        resolver_version=RESOLVER_VERSION,
        reason=reason,
        corrected_at=datetime.now(timezone.utc),
    )
    session.add(correction)
    session.flush()

    game.week_number = authoritative_week
    session.flush()
    return correction


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audited correction of a game's week_number")
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--expect-current-week", required=True, type=int,
        help="the week the inspector showed. The repair refuses if the row says "
             "anything else, so you correct the row you reviewed.",
    )
    parser.add_argument("--authoritative-week", required=True, type=int)
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually correct the row. WITHOUT this flag nothing is written.",
    )
    args = parser.parse_args(argv)

    with session_scope() as session:
        plan = plan_repair(
            session, game_id=args.game_id,
            expected_current_week=args.expect_current_week,
            authoritative_week=args.authoritative_week,
            reason=args.reason,
        )
        print(plan.render())

    if not plan.safe:
        print()
        print("REFUSED — nothing written.")
        return 1
    if not args.apply:
        print()
        print("DRY RUN — nothing written. Re-run with --apply to correct it.")
        return 0

    with session_scope() as session:
        correction = apply_repair(
            session, game_id=args.game_id,
            expected_current_week=args.expect_current_week,
            authoritative_week=args.authoritative_week,
            reason=args.reason,
        )
        print()
        print(f"APPLIED. Audit row {correction.id}: week_number "
              f"{correction.old_value} -> {correction.new_value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
