"""Season/rules/week persistence. Thin CRUD + query only — no business
rules (those live in app/domain and app/services)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.season import Season as SeasonRow
from app.db.models.season import SeasonRules as SeasonRulesRow
from app.db.models.season import Week as WeekRow
from app.domain.models import SeasonRules as DomainSeasonRules
from app.marketdata.provenance import SYNTHETIC_SOURCE


class WeekConfigurationConflict(RuntimeError):
    """An existing week row contradicts what was asked for."""


class SeasonRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_season(self, *, name: str, year: int, status: str = "ACTIVE") -> SeasonRow:
        row = SeasonRow(name=name, year=year, status=status)
        self.session.add(row)
        self.session.flush()
        return row

    def create_season_rules(
        self,
        *,
        season_id: uuid.UUID,
        rules: DomainSeasonRules,
        effective_from: datetime,
        market_data_provider: str = SYNTHETIC_SOURCE,
        roster_data_provider: str = SYNTHETIC_SOURCE,
    ) -> SeasonRulesRow:
        row = SeasonRulesRow(
            season_id=season_id,
            rules_version=rules.rules_version,
            starting_bankroll_cents=rules.starting_bankroll.cents,
            canonical_sportsbook="DRAFTKINGS",
            market_data_provider=market_data_provider,
            roster_data_provider=roster_data_provider,
            research_settlement_provider="NFL_OFFICIAL_STATS",
            research_settlement_delay_hours=72,
            supported_prop_types=["passing_yards", "passing_touchdowns", "rushing_yards", "receptions", "receiving_yards"],
            devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=10,
            batch_methodology="BATCH_SMALL",
            checkpoint_windows={
                "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
                "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
                "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
            },
            kelly_fraction=rules.kelly_fraction,
            standard_max_bankroll_fraction=rules.standard_max_bankroll_fraction,
            exceptional_max_bankroll_fraction=rules.exceptional_max_bankroll_fraction,
            minimum_stake_cents=rules.minimum_stake.cents,
            stake_increment_cents=rules.stake_increment.cents,
            pounce_limit=rules.pounce_limit,
            attribution_confidence_threshold=0.75,
            competition_requires_nonpushable_line=True,
            effective_from=effective_from,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_season(self, season_id: uuid.UUID) -> SeasonRow:
        row = self.session.get(SeasonRow, season_id)
        if row is None:
            raise LookupError(f"season {season_id} not found")
        return row

    def get_active_rules(self, season_id: uuid.UUID) -> SeasonRulesRow:
        stmt = (
            select(SeasonRulesRow)
            .where(SeasonRulesRow.season_id == season_id, SeasonRulesRow.superseded_by.is_(None))
            .order_by(SeasonRulesRow.effective_from.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise LookupError(f"no active season_rules for season {season_id}")
        return row

    def prepare_week(
        self, *, season_id: uuid.UUID, week_number: int, is_real_money: bool
    ) -> WeekRow:
        """Create the week PENDING. Creation is not opening.

        These were one operation, which forced an ordering the
        precommitment design cannot accept: `BenchmarkSlatePlan.week_id`
        is required BEFORE the first OPENING checkpoint, so committing a
        research slate meant declaring the competition week open first.
        Preparing a week creates the FK target and emits no event, moves no
        bankroll and touches no competitor.

        Idempotent for an identical PENDING week; a contradictory existing
        row is refused rather than adjusted, because `is_real_money` decides
        whether a week's results count and silently changing it under a
        committed slate would rewrite what the season agreed to.
        """

        existing = self.session.execute(
            select(WeekRow).where(
                WeekRow.season_id == season_id, WeekRow.week_number == week_number
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.is_real_money != is_real_money:
                raise WeekConfigurationConflict(
                    f"week {week_number} already exists with "
                    f"is_real_money={existing.is_real_money}, not {is_real_money}. "
                    "That flag decides whether the week counts; it is not "
                    "adjusted in place."
                )
            return existing

        row = WeekRow(
            season_id=season_id,
            week_number=week_number,
            is_real_money=is_real_money,
            status="PENDING",
            opened_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def open_week(self, *, season_id: uuid.UUID, week_number: int, is_real_money: bool, opened_at: datetime) -> WeekRow:
        """Prepare if needed, then TRANSITION to OPENED.

        Creation and opening are two steps now; this keeps the old
        single-call ergonomics by doing both, so existing callers are
        unchanged while production can prepare a week without opening it.
        """

        row = self.prepare_week(
            season_id=season_id, week_number=week_number, is_real_money=is_real_money
        )
        if row.status == "OPENED":
            return row
        if row.status != "PENDING":
            raise WeekConfigurationConflict(
                f"week {week_number} is {row.status}; only a PENDING week opens"
            )
        row.status = "OPENED"
        row.opened_at = opened_at
        self.session.flush()
        return row

    def get_week(self, week_id: uuid.UUID) -> WeekRow:
        row = self.session.get(WeekRow, week_id)
        if row is None:
            raise LookupError(f"week {week_id} not found")
        return row

    def close_week(self, week_id: uuid.UUID, *, closed_at: datetime) -> WeekRow:
        row = self.get_week(week_id)
        row.status = "CLOSED"
        row.closed_at = closed_at
        self.session.flush()
        return row
