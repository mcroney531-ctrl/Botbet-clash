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
from app.domain.week_profile import WeekFlags, WeekProfile, flags_for, profile_of
from app.marketdata.provenance import SYNTHETIC_SOURCE


class WeekConfigurationConflict(RuntimeError):
    """An existing week row contradicts what was asked for."""


def week_flags(row: WeekRow) -> WeekFlags:
    """The three durable booleans, read off a persisted week."""

    return WeekFlags(
        is_real_money=row.is_real_money,
        counts_toward_standings=row.counts_toward_standings,
        counts_toward_awards=row.counts_toward_awards,
    )


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
        self, *, season_id: uuid.UUID, week_number: int, profile: WeekProfile
    ) -> WeekRow:
        """Create the week PENDING. Creation is not opening.

        These were one operation, which forced an ordering the
        precommitment design cannot accept: `BenchmarkSlatePlan.week_id`
        is required BEFORE the first OPENING checkpoint, so committing a
        research slate meant declaring the competition week open first.
        Preparing a week creates the FK target and emits no event, moves no
        bankroll and touches no competitor.

        Takes a PROFILE, not three loose booleans. All three flags are
        frozen together, and idempotency requires ALL of them to match: a
        row that agrees about money but disagrees about standings is not
        the same week, and adjusting it in place would rewrite what the
        season already agreed to.

        **The SEASON row is locked first.** The existence check has to
        happen under a lock, and there is no week row to lock yet -- so two
        concurrent callers would both miss and both insert, and one would
        take a raw `IntegrityError` from the unique
        `(season_id, week_number)` index. Locking the durable parent
        serializes week creation for this season and removes the race
        rather than recovering from it. The unique index remains the hard
        backstop; nothing here depends on catching its violation.

        The lock is held for two local statements and no network call.
        """

        flags = flags_for(profile)

        # The parent, FOR UPDATE, BEFORE the existence check. Order is the
        # whole point: checking first and locking after would leave exactly
        # the window this closes.
        season = self.session.execute(
            select(SeasonRow).where(SeasonRow.id == season_id).with_for_update()
        ).scalar_one_or_none()
        if season is None:
            raise LookupError(f"season {season_id} not found")

        existing = self.session.execute(
            select(WeekRow).where(
                WeekRow.season_id == season_id, WeekRow.week_number == week_number
            )
        ).scalar_one_or_none()
        if existing is not None:
            current = week_flags(existing)
            if current != flags:
                raise WeekConfigurationConflict(
                    f"week {week_number} already exists as "
                    f"{profile_of(current) or 'NONSTANDARD'} ({current.describe()}), "
                    f"not {profile} ({flags.describe()}). These flags decide "
                    "whether the week counts; they are not adjusted in place."
                )
            return existing

        row = WeekRow(
            season_id=season_id,
            week_number=week_number,
            is_real_money=flags.is_real_money,
            counts_toward_standings=flags.counts_toward_standings,
            counts_toward_awards=flags.counts_toward_awards,
            status="PENDING",
            opened_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def open_week(
        self, *, season_id: uuid.UUID, week_number: int, profile: WeekProfile,
        opened_at: datetime,
    ) -> tuple[WeekRow, bool]:
        """Prepare if needed, then TRANSITION to OPENED.

        Returns `(row, transitioned)`. The flag is what the caller needs to
        publish `WEEK_OPENED` exactly once: the repository was already
        idempotent for an OPENED row, but its caller published an event on
        every call, so a second open emitted a second "the week opened"
        into the competition log for something that did not happen.

        The row is taken `FOR UPDATE` first, so two concurrent opens
        serialize and only one of them sees PENDING. Relying on Python call
        ordering would make the duplicate event a race rather than a bug.
        """

        self.prepare_week(
            season_id=season_id, week_number=week_number, profile=profile
        )
        row = self.session.execute(
            select(WeekRow)
            .where(WeekRow.season_id == season_id, WeekRow.week_number == week_number)
            .with_for_update()
        ).scalar_one()

        if row.status == "OPENED":
            return row, False
        if row.status != "PENDING":
            raise WeekConfigurationConflict(
                f"week {week_number} is {row.status}; only a PENDING week opens. "
                "A closed week is not reopened -- its results are already part "
                "of the season record."
            )
        row.status = "OPENED"
        row.opened_at = opened_at
        self.session.flush()
        return row, True

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
