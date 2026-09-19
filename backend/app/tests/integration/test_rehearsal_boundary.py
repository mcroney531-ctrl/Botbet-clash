"""Phase 4A.8: a rehearsal week exercises the whole lifecycle and cannot
move the official bankroll.

Before this phase `Week.is_real_money` was a label. Nothing read it:
`issue_ticket` accepted a rehearsal week, `record_execution(PLACED)` wrote
a STAKE transaction against it, `settle_wager` credited WIN_RETURN, and
`LedgerRepository.record` persisted whatever it was handed. Week 4 is a
REHEARSAL week that was about to be opened.

The fix is NOT to switch competition off during a rehearsal. CONSTITUTION.md
§6 and RULES.md §3 require the rehearsal week to exercise stake
calculations, execution, settlement, receipts and audit reconstruction, and
a rehearsal that skips the logic proves nothing about the logic. So the
same validators run either way and the branch is at the SIDE-EFFECT
boundary alone.

The lettered tests below are the acceptance list from the phase brief.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.core.money import Money
from app.db.models.competition import Wager as WagerRow
from app.db.models.settlement import BankrollTransaction as BankrollTransactionRow
from app.db.repositories.ledger_repository import LedgerRepository
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import (
    CompetitorStatus,
    RiskPosture,
    Side,
    SportsbookResult,
    Urgency,
    WagerExecutionStatus,
)
from app.domain.errors import (
    DuplicateWeeklyDecision,
    DuplicateSettlement,
    InvalidStakeIncrement,
    InvalidStateTransition,
    LineOutsideAcceptableBoundary,
    PriceOutsideAcceptableBoundary,
    StakeBelowMinimum,
    StakeExceedsCap,
)
from app.domain.models import SeasonRules
from app.domain.rehearsal import RehearsalBoundaryViolation
from app.domain.week_profile import WeekProfile
from app.services.season_commissioner import SeasonCommissioner
from app.tests.integration.backstops import backstops_down, corrupt_profile, force_week

# Anything Postgres raises out of a plpgsql RAISE EXCEPTION. SQLAlchemy
# wraps `raise_exception` as ProgrammingError; the broader bases are listed
# so a driver or dialect change cannot turn a refusal into a pass.
DB_REFUSAL = (ProgrammingError, IntegrityError, DBAPIError)


def _rules(version: str) -> SeasonRules:
    return SeasonRules(
        rules_version=version,
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


class Fixture:
    """One season with BOTH week shapes open at once.

    Deliberately both: most of these tests are a claim about the
    DIFFERENCE between the two paths, and a difference is only visible
    when the same season, the same competitor, the same rules and the same
    market shape are on both sides of it.
    """

    _n = 0

    def __init__(self) -> None:
        Fixture._n += 1
        tag = f"{Fixture._n}-{uuid.uuid4().hex[:8]}"
        self.commissioner = SeasonCommissioner.create_season(
            name=f"Rehearsal Boundary {tag}", year=2026, rules=_rules(f"rehearsal-{tag}")
        )
        self.season_id = self.commissioner.season_id
        self.competitor = self.commissioner.register_competitor(
            competitor_id="anthropic", provider="Anthropic",
            display_name="Anthropic", model_identifier="x", model_version="v1",
        )
        self.competitive_week = self.commissioner.open_week(
            week_number=1, profile=WeekProfile.COMPETITIVE
        )
        self.rehearsal_week = self.commissioner.open_week(
            week_number=0, profile=WeekProfile.REHEARSAL
        )
        self._markets = 0

    def market(self, week_number: int) -> str:
        self._markets += 1
        ref = f"g{self._markets}-{uuid.uuid4().hex[:8]}"
        with session_scope() as session:
            repo = MarketRepository(session)
            game = repo.create_game(
                external_ref=ref, season_id=self.season_id, week_number=week_number,
                home_team="KC", away_team="BUF",
                kickoff_at=datetime.now(timezone.utc) + timedelta(days=3),
            )
            player = repo.create_player(
                external_ref=f"p-{ref}", name="Player X", team="KC", position="WR"
            )
            repo.create_game_player(game_id=game.id, player_id=player.id, team=game.home_team_canonical)
            return str(repo.create_prop_market(
                game_id=game.id, player_id=player.id, stat_type="receiving_yards"
            ).id)

    def ticket(self, *, rehearsal: bool, **overrides) -> str:
        week_number = 0 if rehearsal else 1
        kwargs = dict(
            week_id=self.rehearsal_week if rehearsal else self.competitive_week,
            season_competitor_id=self.competitor,
            market_id=self.market(week_number),
            side=Side.OVER,
            urgency=Urgency.STRONG,
            risk_posture=RiskPosture.STANDARD,
            model_probability_over=Decimal("0.61"),
            price_for_side=-115,
            observed_line=Decimal("52.5"),
            model_requested_stake=Money.from_dollars_str("2.00"),
            why_now="test",
        )
        kwargs.update(overrides)
        return self.commissioner.issue_ticket(**kwargs)

    def execute(self, ticket_id: str, *, rehearsal: bool, **overrides) -> str:
        kwargs = dict(
            ticket_id=ticket_id,
            status=(
                WagerExecutionStatus.SIMULATED if rehearsal
                else WagerExecutionStatus.PLACED
            ),
            sportsbook="DRAFTKINGS",
            actual_line=Decimal("52.5"),
            actual_price=-115,
            actual_stake=Money.from_dollars_str("2.00"),
        )
        kwargs.update(overrides)
        return self.commissioner.record_execution(**kwargs)

    def balance(self) -> Money:
        return self.commissioner.available_balance(self.competitor)

    def transactions(self, kind: str | None = None):
        txns = self.commissioner.transactions_for(self.competitor)
        return [t for t in txns if kind is None or t.type.value == kind]

    def week_events(self, *, rehearsal: bool):
        week_id = self.rehearsal_week if rehearsal else self.competitive_week
        return self.commissioner.events_for_week(week_id)

    def event_types(self, *, rehearsal: bool) -> set[str]:
        return {e.event_type.value for e in self.week_events(rehearsal=rehearsal)}


# ======================================================================
# A / B -- both weeks still issue tickets
# ======================================================================


def test_A_competitive_ticket_issuance_still_works():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)

    assert fix.commissioner.get_ticket(ticket_id).status.value == "ISSUED"
    assert "TICKET_LOCKED" in fix.event_types(rehearsal=False)


def test_B_rehearsal_ticket_issuance_still_works():
    """A rehearsal ticket is ISSUED through the same path -- candidate
    selection, Kelly sizing, the caps and the Pounce limit are exactly what
    the rehearsal exists to exercise."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    ticket = fix.commissioner.get_ticket(ticket_id)

    assert ticket.status.value == "ISSUED"
    assert ticket.final_allowed_stake > Money.zero(), "Kelly/cap math did not run"


def test_a_rehearsal_ticket_is_never_announced_as_an_actionable_one():
    """The event TYPE differs, not just a field inside the payload.

    A consumer that has never heard of rehearsal ignores an unknown event
    type. The same consumer would act on a TICKET_LOCKED whose payload
    carried a mode field it does not read -- which is the whole hazard.
    """

    fix = Fixture()
    fix.ticket(rehearsal=True)
    fix.ticket(rehearsal=False)

    rehearsal = fix.event_types(rehearsal=True)
    competitive = fix.event_types(rehearsal=False)

    assert "SIMULATED_TICKET_LOCKED" in rehearsal
    assert "TICKET_LOCKED" not in rehearsal
    assert "TICKET_LOCKED" in competitive
    assert "SIMULATED_TICKET_LOCKED" not in competitive


def test_a_rehearsal_pounce_is_also_announced_as_simulated():
    fix = Fixture()
    fix.ticket(rehearsal=True, urgency=Urgency.POUNCE, risk_posture=RiskPosture.AGGRESSIVE)

    types = fix.event_types(rehearsal=True)
    assert "SIMULATED_POUNCE_ISSUED" in types
    assert "POUNCE_ISSUED" not in types


def test_every_week_scoped_event_carries_the_week_mode():
    """The stamp is injected centrally in `_publish`, so no event type can
    be added without it -- including ones nobody has thought of yet."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    fix.execute(ticket_id, rehearsal=True)

    events = fix.week_events(rehearsal=True)
    assert events, "no events to check"
    for event in events:
        assert event.payload.get("week_mode") == "REHEARSAL", (
            f"{event.event_type.value} was published without a week mode"
        )

    for event in fix.week_events(rehearsal=False):
        assert event.payload.get("week_mode") == "COMPETITIVE"


def test_a_rehearsal_ticket_payload_says_do_not_place():
    fix = Fixture()
    fix.ticket(rehearsal=True)
    locked = [
        e for e in fix.week_events(rehearsal=True)
        if e.event_type.value == "SIMULATED_TICKET_LOCKED"
    ]
    assert locked and "DO NOT PLACE" in locked[0].payload["advisory"]


# ======================================================================
# C / D -- neither status is silently translated
# ======================================================================


def test_C_rehearsal_PLACED_execution_is_refused():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)

    with pytest.raises(RehearsalBoundaryViolation) as excinfo:
        fix.execute(ticket_id, rehearsal=True, status=WagerExecutionStatus.PLACED)

    assert "REHEARSAL" in str(excinfo.value)
    assert fix.balance() == Money.from_dollars_str("15.00")
    with session_scope() as session:
        assert session.execute(
            select(WagerRow).where(WagerRow.ticket_id == uuid.UUID(ticket_id))
        ).scalars().all() == []


def test_D_competitive_SIMULATED_execution_is_refused():
    """The refusal is symmetric on purpose. A competitive week that
    accepted SIMULATED would hold a ticket it believed was executed with
    no wager standing at any book."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)

    with pytest.raises(RehearsalBoundaryViolation):
        fix.execute(ticket_id, rehearsal=False, status=WagerExecutionStatus.SIMULATED)

    assert fix.commissioner.get_ticket(ticket_id).status.value == "ISSUED"


def test_neither_status_is_silently_translated_into_the_other():
    """The caller has to learn it asked for the wrong operation. A service
    that quietly turned PLACED into SIMULATED would leave a competitive
    week believing it had a real wager on."""

    fix = Fixture()
    rehearsal_ticket = fix.ticket(rehearsal=True)
    with pytest.raises(RehearsalBoundaryViolation):
        fix.execute(rehearsal_ticket, rehearsal=True, status=WagerExecutionStatus.PLACED)

    # Not translated to SIMULATED behind the caller's back.
    with session_scope() as session:
        assert session.execute(select(WagerRow).where(
            WagerRow.week_id == uuid.UUID(fix.rehearsal_week)
        )).scalars().all() == []


# ======================================================================
# E -- shared validation
# ======================================================================


@pytest.mark.parametrize(
    "overrides, expected",
    [
        pytest.param(
            {"actual_stake": Money.from_dollars_str("14.00")}, StakeExceedsCap,
            id="stake above the ticket cap",
        ),
        pytest.param(
            {"actual_stake": Money(10)}, StakeBelowMinimum, id="stake below the minimum",
        ),
        pytest.param(
            {"actual_stake": Money(130)}, InvalidStakeIncrement, id="stake off the increment",
        ),
        pytest.param({"actual_stake": None}, ValueError, id="stake missing"),
        pytest.param({"actual_line": None}, ValueError, id="line missing"),
        pytest.param({"actual_price": None}, ValueError, id="price missing"),
    ],
)
def test_E_rehearsal_simulated_execution_runs_the_same_validators(overrides, expected):
    """These all used to be inside `if status is PLACED`. A simulated
    execution would have skipped the stake cap, the increment, the line
    boundary and the price boundary -- so the rehearsal would have proved
    nothing about any of them."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)

    with pytest.raises(expected):
        fix.execute(ticket_id, rehearsal=True, **overrides)


def test_E_rehearsal_simulated_execution_enforces_the_line_boundary():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True, acceptable_line_boundary=Decimal("53.5"))

    with pytest.raises(LineOutsideAcceptableBoundary):
        fix.execute(ticket_id, rehearsal=True, actual_line=Decimal("56.5"))


def test_E_rehearsal_simulated_execution_enforces_the_price_boundary():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True, worst_acceptable_price=-120)

    with pytest.raises(PriceOutsideAcceptableBoundary):
        fix.execute(ticket_id, rehearsal=True, actual_price=-160)


def test_E_the_two_paths_share_one_validation_body():
    """Structural, not behavioural: the same source lines run for both.

    Parallel implementations pass identical behavioural tests on the day
    they are written and drift apart afterwards, so the claim worth
    testing is that there is only ONE body to drift.
    """

    import ast
    import inspect

    from app.services.season_commissioner import SeasonCommissioner as SC

    tree = ast.parse(inspect.getsource(SC.record_execution).strip())
    guards = [
        ast.unparse(node.test)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
    ]
    # The validation block is gated on `.executes` (both statuses) and the
    # money block on `.moves_money` (PLACED alone). If someone regates the
    # validation on the money predicate, the rehearsal stops validating.
    assert "status.executes" in guards, (
        "the shared validation block is no longer gated on status.executes"
    )
    assert "status.moves_money" in guards, (
        "the side-effect block is no longer gated on status.moves_money"
    )
    assert "status is WagerExecutionStatus.PLACED" not in guards, (
        "validation was regated on PLACED; a simulated execution now skips it"
    )


# ======================================================================
# F / G -- a simulated execution is auditable and costs nothing
# ======================================================================


def test_F_simulated_execution_creates_one_auditable_wager_record():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    wager_id = fix.execute(ticket_id, rehearsal=True)

    wager = fix.commissioner.get_wager(wager_id)
    assert wager.execution_status is WagerExecutionStatus.SIMULATED
    assert wager.actual_stake == Money.from_dollars_str("2.00")
    assert wager.actual_line == Decimal("52.50")
    assert wager.actual_price == -115
    assert wager.execution_timestamp is not None
    assert fix.commissioner.get_ticket(ticket_id).status.value == "EXECUTED"

    with session_scope() as session:
        row = session.get(WagerRow, uuid.UUID(wager_id))
        assert row.sportsbook == "DRAFTKINGS"
        # The rehearsal DID consult the bankroll -- that is what the stake
        # cap was checked against -- so the figure is recorded. Without it
        # an audit cannot reconstruct why the stake was accepted.
        assert row.bankroll_at_execution_cents == 1500


def test_G_simulated_execution_creates_zero_bankroll_transactions():
    fix = Fixture()
    before = fix.balance()
    ticket_id = fix.ticket(rehearsal=True)
    fix.execute(ticket_id, rehearsal=True)

    assert fix.transactions("STAKE") == []
    assert fix.balance() == before == Money.from_dollars_str("15.00")


def test_G_the_simulated_execution_event_is_a_distinct_type():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    fix.execute(ticket_id, rehearsal=True)

    types = fix.event_types(rehearsal=True)
    assert "SIMULATED_BET_EXECUTED" in types
    assert "BET_EXECUTED" not in types
    assert "BANKROLL_CHANGED" not in types


# ======================================================================
# H -- M : settlement rehearses without paying
# ======================================================================


def _simulated_wager(fix: Fixture) -> str:
    return fix.execute(fix.ticket(rehearsal=True), rehearsal=True)


def test_H_simulated_settlement_persists_its_result():
    fix = Fixture()
    wager_id = _simulated_wager(fix)

    settlement_id = fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )

    assert settlement_id
    from app.db.models.settlement import Settlement as SettlementRow

    with session_scope() as session:
        row = session.get(SettlementRow, uuid.UUID(settlement_id))
        assert row is not None
        assert row.sportsbook_result == "WIN"
        assert row.sportsbook_payout_cents == 374


@pytest.mark.parametrize(
    "result, forbidden_type",
    [
        pytest.param(SportsbookResult.WIN, "WIN_RETURN", id="I win"),
        pytest.param(SportsbookResult.PUSH, "PUSH_RETURN", id="J push"),
        pytest.param(SportsbookResult.VOID, "VOID_RETURN", id="K void"),
    ],
)
def test_IJK_simulated_settlement_credits_nothing(result, forbidden_type):
    fix = Fixture()
    wager_id = _simulated_wager(fix)
    before = fix.balance()

    fix.commissioner.settle_wager(
        wager_id=wager_id, result=result, payout=Money.from_dollars_str("3.74")
    )

    assert fix.transactions(forbidden_type) == []
    assert fix.transactions() == fix.transactions("SEASON_START")
    assert fix.balance() == before, "the official bankroll moved during a rehearsal"


def test_L_simulated_settlement_emits_no_real_bankroll_changed():
    fix = Fixture()
    wager_id = _simulated_wager(fix)

    fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )

    types = fix.event_types(rehearsal=True)
    assert "SIMULATED_SETTLED" in types
    assert "BANKROLL_CHANGED" not in types
    assert "PROP_WON" not in types, (
        "a rehearsal outcome must not enter the competition log as a real one"
    )


def test_M_simulated_settlement_cannot_bust_a_competitor():
    """A rehearsal LOSS is the dangerous case: it is the one that, on the
    real path, walks the bankroll toward zero and flips the competitor to
    BUSTED for the rest of the season."""

    fix = Fixture()
    wager_id = _simulated_wager(fix)

    fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.LOSS, payout=Money.zero()
    )

    assert fix.commissioner.competitor_status(fix.competitor) is CompetitorStatus.ACTIVE
    assert fix.balance() == Money.from_dollars_str("15.00")


def test_S_repeated_simulated_settlement_is_still_duplicate_safe():
    fix = Fixture()
    wager_id = _simulated_wager(fix)
    fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )

    with pytest.raises(DuplicateSettlement):
        fix.commissioner.settle_wager(
            wager_id=wager_id, result=SportsbookResult.WIN,
            payout=Money.from_dollars_str("3.74"),
        )

    assert fix.balance() == Money.from_dollars_str("15.00")


def test_a_wager_that_never_executed_is_still_unsettleable():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    wager_id = fix.execute(
        ticket_id, rehearsal=True, status=WagerExecutionStatus.MARKET_MOVED,
        actual_stake=None, actual_line=None, actual_price=None,
    )

    with pytest.raises(InvalidStateTransition):
        fix.commissioner.settle_wager(
            wager_id=wager_id, result=SportsbookResult.WIN, payout=Money(100)
        )


# ======================================================================
# N / O -- the ledger boundary itself
# ======================================================================


@pytest.mark.parametrize(
    "txn_type, amount",
    [
        pytest.param("STAKE", -200, id="N stake"),
        pytest.param("WIN_RETURN", 374, id="O win return"),
        pytest.param("PUSH_RETURN", 200, id="push return"),
        pytest.param("VOID_RETURN", 200, id="void return"),
        pytest.param("ADJUSTMENT", -50, id="adjustment"),
    ],
)
def test_NO_the_ledger_repository_refuses_rehearsal_money_directly(txn_type, amount):
    """Bypasses the Commissioner entirely. `LedgerRepository.record` is
    itself a financial mutation point -- anything holding a session can
    call it, and a future service that forgot the profile check would move
    real money on a rehearsal week with nothing in the way.

    ADJUSTMENT is in this list deliberately. It mutates the same SUM as
    every other type, and "it's only an adjustment" is exactly how a
    rehearsal leaks into the competitive ledger.
    """

    fix = Fixture()
    with pytest.raises(RehearsalBoundaryViolation):
        with session_scope() as session:
            LedgerRepository(session).record(BankrollTransactionRow(
                season_competitor_id=uuid.UUID(fix.competitor),
                week_id=uuid.UUID(fix.rehearsal_week),
                type=txn_type, amount_cents=amount, reason="bypass attempt",
            ))

    assert fix.balance() == Money.from_dollars_str("15.00")


def test_the_ledger_guard_covers_every_type_the_balance_sums():
    """The refusal list and the balance query must agree.

    A type the balance counts but the guard ignores is a hole; a type the
    guard blocks but the balance ignores is dead weight. Both are found by
    comparing the two sets rather than by remembering to update both.
    """

    import ast
    import inspect

    from app.domain.enums import BankrollTransactionType
    from app.domain.rehearsal import BANKROLL_ALTERING_TYPES

    every_type = {t.value for t in BankrollTransactionType}
    assert BANKROLL_ALTERING_TYPES <= every_type, (
        f"the guard names types that do not exist: "
        f"{BANKROLL_ALTERING_TYPES - every_type}"
    )

    # `available_balance` sums amount_cents with no type filter, so every
    # transaction type alters the bankroll and every one must be guarded.
    source = ast.parse(inspect.getsource(LedgerRepository.available_balance).strip())
    assert "type" not in ast.unparse(source).replace("txn_type", ""), (
        "available_balance now filters by type; the guard list must be "
        "narrowed to match instead of blocking types the balance ignores"
    )
    assert BANKROLL_ALTERING_TYPES == every_type, (
        f"unguarded types that still change the balance: "
        f"{every_type - BANKROLL_ALTERING_TYPES}"
    )


def test_the_ledger_guard_refuses_a_week_that_matches_no_profile():
    """A half-rehearsal is not a rehearsal with a typo. The guard refuses
    rather than resolving it to the nearest profile, because resolving
    would decide, on the season's behalf, that money may move.

    The database now makes such a week unrepresentable, so this has to
    take the backstops down to build one. That is the point: the service
    guard is what remains if the schema is ever wrong, and a layer nobody
    tests without the layer above it is a layer nobody has tested.
    """

    fix = Fixture()
    with backstops_down():
        pass  # proves the constraint is installed; force_week arms it again
    nonstandard = force_week(
        fix.season_id, 9, is_real_money=False, counts_toward_standings=True,
        counts_toward_awards=False, status='OPENED',
    )

    with pytest.raises(RehearsalBoundaryViolation):
        with session_scope() as session:
            LedgerRepository(session).record(BankrollTransactionRow(
                season_competitor_id=uuid.UUID(fix.competitor),
                week_id=nonstandard, type="STAKE", amount_cents=-100,
                reason="nonstandard week",
            ))


# ======================================================================
# P / Q / R -- the competitive path is untouched
# ======================================================================


def test_P_real_competitive_stake_still_works():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)
    fix.execute(ticket_id, rehearsal=False)

    stakes = fix.transactions("STAKE")
    assert len(stakes) == 1
    assert stakes[0].amount == Money.from_dollars_str("-2.00")
    assert fix.balance() == Money.from_dollars_str("13.00")
    assert "BET_EXECUTED" in fix.event_types(rehearsal=False)


def test_Q_real_competitive_settlement_credit_still_works():
    fix = Fixture()
    wager_id = fix.execute(fix.ticket(rehearsal=False), rehearsal=False)

    fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )

    assert len(fix.transactions("WIN_RETURN")) == 1
    assert fix.balance() == Money.from_dollars_str("16.74")
    types = fix.event_types(rehearsal=False)
    assert {"PROP_WON", "BANKROLL_CHANGED"} <= types
    assert "SIMULATED_SETTLED" not in types


def test_R_season_start_funding_with_a_null_week_still_works():
    """Season funding is not a week's money. Both the repository guard and
    the database trigger pass `week_id IS NULL` straight through -- if they
    did not, no competitor could ever be funded."""

    fix = Fixture()
    funding = fix.transactions("SEASON_START")

    assert len(funding) == 1
    assert funding[0].amount == Money.from_dollars_str("15.00")

    with session_scope() as session:
        rows = session.execute(
            select(BankrollTransactionRow).where(
                BankrollTransactionRow.season_competitor_id == uuid.UUID(fix.competitor),
                BankrollTransactionRow.type == "SEASON_START",
            )
        ).scalars().all()
        assert [r.week_id for r in rows] == [None]


def test_R_a_null_week_transaction_passes_the_repository_guard():
    fix = Fixture()
    with session_scope() as session:
        LedgerRepository(session).record(BankrollTransactionRow(
            season_competitor_id=uuid.UUID(fix.competitor),
            week_id=None, type="ADJUSTMENT", amount_cents=-100,
            reason="season-level correction",
        ))

    assert fix.balance() == Money.from_dollars_str("14.00")


# ======================================================================
# T -- PASS is a rehearsal-legal decision
# ======================================================================


def test_T_rehearsal_pass_remains_valid():
    """PASS has no bankroll side effect and is part of the weekly decision
    logic the rehearsal is meant to exercise."""

    fix = Fixture()
    pass_id = fix.commissioner.record_pass(
        week_id=fix.rehearsal_week, season_competitor_id=fix.competitor,
        reason_for_pass="no edge anywhere on the slate",
    )

    assert pass_id
    assert fix.balance() == Money.from_dollars_str("15.00")
    passes = [
        e for e in fix.week_events(rehearsal=True)
        if e.event_type.value == "PASS_DECLARED"
    ]
    # ONE type, not two. A pass is the ABSENCE of a wager -- there is
    # nothing in it for a consumer to misread as an instruction to act --
    # so it is distinguished by `week_mode` rather than by a separate type.
    assert len(passes) == 1
    assert passes[0].payload["week_mode"] == "REHEARSAL"


# ======================================================================
# U -- the Forecast Lab never learns what a week profile is
# ======================================================================


def test_U_forecast_lab_never_inspects_the_week_profile():
    """A byte-identical research week is the goal. Registration, market
    ingestion, checkpoint capture, evidence, benchmark resolution,
    forecasting, research settlement and scoring are research operations,
    not real-money mutations, and none of them may acquire an opinion
    about whether the week is a rehearsal.
    """

    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    forbidden = (
        "WeekProfile", "profile_of", "may_move_money", "RehearsalBoundaryViolation",
        "alters_official_bankroll", "is_real_money",
    )
    offenders = []
    for package in ("forecast_lab", "marketdata", "rosterdata", "scheduledata", "ai"):
        for path in sorted((root / package).rglob("*.py")):
            tree = ast.parse(path.read_text())
            names = {
                n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            }
            hits = sorted(set(forbidden) & names)
            if hits:
                offenders.append(f"{path.relative_to(root)}: {hits}")

    # week_readiness REPORTS the profile -- it is an operator report about
    # the week, not a research operation, and it decides nothing. Anything
    # else appearing here means a research path learned to gate on money.
    offenders = [o for o in offenders if not o.startswith("forecast_lab/week_readiness.py")]
    assert offenders == [], (
        "Forecast Lab code is inspecting the money boundary:\n  " + "\n  ".join(offenders)
    )


def test_U_registering_a_game_against_a_rehearsal_week_is_unaffected():
    """The narrowest end-to-end version of the same claim: research data
    lands on a rehearsal week exactly as it lands on a competitive one."""

    fix = Fixture()
    market_id = fix.market(0)

    from app.db.models.markets import PropMarket

    with session_scope() as session:
        assert session.get(PropMarket, uuid.UUID(market_id)) is not None


# ======================================================================
# The database backstop -- bypassing every line of Python above
# ======================================================================


def test_the_database_refuses_a_rehearsal_stake_written_by_raw_orm():
    """The claim the trigger exists for: a rehearsal week cannot move the
    official bankroll EVEN IF a future service bypasses the Commissioner
    and the repository entirely.

    This is `session.add` straight onto the table. No service, no
    repository, no domain object -- nothing that could have been taught
    about week profiles.
    """

    fix = Fixture()
    with pytest.raises(DB_REFUSAL) as excinfo:
        with session_scope() as session:
            session.add(BankrollTransactionRow(
                season_competitor_id=uuid.UUID(fix.competitor),
                week_id=uuid.UUID(fix.rehearsal_week),
                type="STAKE", amount_cents=-200, reason="raw bypass",
            ))

    assert "rehearsal week" in str(excinfo.value)
    assert fix.balance() == Money.from_dollars_str("15.00")


@pytest.mark.parametrize(
    "txn_type",
    ["STAKE", "WIN_RETURN", "PUSH_RETURN", "VOID_RETURN", "ADJUSTMENT", "SEASON_START"],
)
def test_the_database_refuses_every_bankroll_altering_type(txn_type):
    fix = Fixture()
    with pytest.raises(DB_REFUSAL):
        with session_scope() as session:
            session.add(BankrollTransactionRow(
                season_competitor_id=uuid.UUID(fix.competitor),
                week_id=uuid.UUID(fix.rehearsal_week),
                type=txn_type, amount_cents=100, reason="raw bypass",
            ))


def test_the_database_refuses_a_rehearsal_stake_smuggled_in_by_update():
    """BEFORE UPDATE, not just BEFORE INSERT. A row inserted legitimately
    against a competitive week and then repointed at a rehearsal week
    would otherwise walk straight past an insert-only trigger."""

    fix = Fixture()
    fix.execute(fix.ticket(rehearsal=False), rehearsal=False)

    with pytest.raises(DB_REFUSAL):
        with session_scope() as session:
            row = session.execute(
                select(BankrollTransactionRow).where(
                    BankrollTransactionRow.type == "STAKE",
                    BankrollTransactionRow.season_competitor_id == uuid.UUID(fix.competitor),
                )
            ).scalars().one()
            row.week_id = uuid.UUID(fix.rehearsal_week)


def test_the_database_refuses_a_placed_wager_on_a_rehearsal_week():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)

    with pytest.raises(DB_REFUSAL) as excinfo:
        with session_scope() as session:
            from app.db.models.competition import Ticket as TicketRow

            ticket = session.get(TicketRow, uuid.UUID(ticket_id))
            session.add(WagerRow(
                ticket_id=ticket.id, season_competitor_id=ticket.season_competitor_id,
                week_id=ticket.week_id, market_id=ticket.market_id,
                requested_stake_cents=200, execution_status="PLACED",
            ))

    assert "rehearsal week" in str(excinfo.value)


def test_the_database_refuses_a_wager_flipped_to_placed_after_the_fact():
    fix = Fixture()
    wager_id = _simulated_wager(fix)

    with pytest.raises(DB_REFUSAL):
        with session_scope() as session:
            session.get(WagerRow, uuid.UUID(wager_id)).execution_status = "PLACED"


def test_the_database_permits_a_simulated_wager_and_a_null_week_transaction():
    """The trigger must not be a blanket refusal. If it were, the tests
    above would pass for the wrong reason and nothing would work."""

    fix = Fixture()
    wager_id = _simulated_wager(fix)
    assert wager_id

    with session_scope() as session:
        session.add(BankrollTransactionRow(
            season_competitor_id=uuid.UUID(fix.competitor),
            week_id=None, type="ADJUSTMENT", amount_cents=-25, reason="season level",
        ))
    assert fix.balance() == Money.from_dollars_str("14.75")


def test_the_trigger_type_list_matches_the_domain_one():
    """Two copies of the same list, in two languages, that must not drift.

    The migration cannot import from `app.domain` -- an applied migration
    is frozen and importing live code would make it mean something
    different later -- so the lists are compared here instead.
    """

    import importlib.util
    import pathlib
    import re

    from app.domain.rehearsal import BANKROLL_ALTERING_TYPES

    path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "alembic" / "versions" / "a3f81c6b57e9_rehearsal_money_backstop.py"
    )
    spec = importlib.util.spec_from_file_location("_rehearsal_backstop", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    in_sql = set(re.findall(r"'([A-Z_]+)'", migration.ALTERING))
    assert in_sql == BANKROLL_ALTERING_TYPES, (
        f"the trigger and the domain guard disagree: "
        f"{in_sql ^ BANKROLL_ALTERING_TYPES}"
    )


def test_both_triggers_are_actually_installed():
    """A migration that was written but never applied would leave every
    database-level test above passing only because the SERVICE refused
    first. This asserts the triggers exist in the database being tested."""

    from sqlalchemy import text

    with session_scope() as session:
        installed = {
            r[0] for r in session.execute(text(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
            ))
        }

    assert "trg_refuse_rehearsal_bankroll_movement" in installed
    assert "trg_refuse_rehearsal_placed_wager" in installed


# ======================================================================
# The census self-audit
# ======================================================================


def test_every_money_writer_in_the_codebase_is_classified():
    """A new writer must be classified rather than silently bypass the
    boundary. Same philosophy as the Game-repair dependency census: prose
    in a review decays, an enumerated set does not."""

    from app.services.money_writer_census import audit_census

    audit = audit_census()
    assert audit.clean, "\n" + audit.render()


def test_no_module_decides_standings_or_awards_without_reading_the_flags():
    """`counts_toward_standings` and `counts_toward_awards` are currently
    read by nothing that computes a result, because nothing computes one
    yet. That is how they become dead metadata and how a rehearsal week
    ends up in the season table. This fails the moment such a module
    appears."""

    from app.services.money_writer_census import audit_standings_flags

    audit = audit_standings_flags()
    assert audit.clean, "\n" + audit.render()




# ======================================================================
# The week profile is FROZEN -- enforced by the database
# ======================================================================
#
# The previous pass built its guards around the idea that an operator can
# flip these three booleans later, and had a test where a COMPETITIVE week
# with a real $2 stake already debited was flipped to REHEARSAL, after
# which settlement suppressed the payout. Nothing was credited, which is
# safe, and the competitor was permanently down two dollars with the
# settlement record calling it a rehearsal, which is not. That is
# impossible state, not supported state, and the database now says so.


@pytest.mark.parametrize(
    "real_money, standings, awards",
    [
        pytest.param(False, False, False, id="competitive to rehearsal"),
        pytest.param(True, True, False, id="one flag: awards"),
        pytest.param(True, False, True, id="one flag: standings"),
        pytest.param(False, True, True, id="one flag: money"),
    ],
)
def test_a_competitive_week_profile_cannot_be_changed(real_money, standings, awards):
    from app.db.models.season import Week as WeekRow

    fix = Fixture()
    with pytest.raises(DB_REFUSAL) as excinfo:
        with session_scope() as session:
            week = session.get(WeekRow, uuid.UUID(fix.competitive_week))
            week.is_real_money = real_money
            week.counts_toward_standings = standings
            week.counts_toward_awards = awards

    assert "frozen at preparation" in str(excinfo.value)


def test_a_rehearsal_week_cannot_be_promoted_to_competitive():
    """The direction that matters most: a rehearsal whose whole point is
    that nothing counts must not be able to become the week that counts,
    retroactively, over artifacts already written against it."""

    from app.db.models.season import Week as WeekRow

    fix = Fixture()
    with pytest.raises(DB_REFUSAL):
        with session_scope() as session:
            week = session.get(WeekRow, uuid.UUID(fix.rehearsal_week))
            week.is_real_money = True
            week.counts_toward_standings = True
            week.counts_toward_awards = True


def test_the_freeze_does_not_touch_the_rest_of_the_week_lifecycle():
    """A blanket "weeks are immutable" would break opening and closing
    them, so the trigger has to be exactly as narrow as the claim."""

    from datetime import datetime as dt

    from app.db.models.season import Week as WeekRow

    fix = Fixture()
    now = dt.now(timezone.utc)
    with session_scope() as session:
        week = session.get(WeekRow, uuid.UUID(fix.rehearsal_week))
        week.status = "CLOSED"
        week.closed_at = now
        week.research_locked_at = now

    with session_scope() as session:
        week = session.get(WeekRow, uuid.UUID(fix.rehearsal_week))
        assert week.status == "CLOSED"
        assert week.closed_at is not None
        assert week.research_locked_at is not None
        assert week.is_real_money is False


def test_opening_and_closing_a_week_through_the_service_still_works():
    fix = Fixture()
    reopened = fix.commissioner.open_week(week_number=0, profile=WeekProfile.REHEARSAL)
    assert reopened == fix.rehearsal_week  # idempotent, not refused


@pytest.mark.parametrize(
    "real_money, standings, awards",
    [
        pytest.param(False, True, True, id="no money but it still counts"),
        pytest.param(True, False, False, id="real money that counts for nothing"),
        pytest.param(True, True, False, id="counts for standings only"),
        pytest.param(False, False, True, id="awards only"),
    ],
)
def test_the_database_refuses_to_create_a_nonstandard_week(real_money, standings, awards):
    """Three free booleans is eight combinations; the governing documents
    describe two. The other six are now unrepresentable."""

    from app.db.models.season import Week as WeekRow

    fix = Fixture()
    with pytest.raises(IntegrityError) as excinfo:
        with session_scope() as session:
            session.add(WeekRow(
                season_id=fix.season_id, week_number=7,
                is_real_money=real_money, counts_toward_standings=standings,
                counts_toward_awards=awards,
            ))

    assert "ck_weeks_reviewed_profile" in str(excinfo.value)


@pytest.mark.parametrize(
    "real_money", [pytest.param(True, id="competitive"), pytest.param(False, id="rehearsal")],
)
def test_both_reviewed_profiles_are_still_creatable(real_money):
    from app.db.models.season import Week as WeekRow

    fix = Fixture()
    with session_scope() as session:
        session.add(WeekRow(
            season_id=fix.season_id, week_number=8,
            is_real_money=real_money, counts_toward_standings=real_money,
            counts_toward_awards=real_money,
        ))


# ======================================================================
# The wager backstop is symmetric
# ======================================================================


def test_the_database_refuses_a_simulated_wager_on_a_competitive_week():
    """The other direction, which the first pass left open. The statuses
    are deliberately symmetric, so the backstop has to be too -- a raw
    writer could otherwise park a SIMULATED wager on the week that counts,
    where it would read as a competitor who never executed."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)

    with pytest.raises(DB_REFUSAL) as excinfo:
        with session_scope() as session:
            from app.db.models.competition import Ticket as TicketRow

            ticket = session.get(TicketRow, uuid.UUID(ticket_id))
            session.add(WagerRow(
                ticket_id=ticket.id, season_competitor_id=ticket.season_competitor_id,
                week_id=ticket.week_id, market_id=ticket.market_id,
                requested_stake_cents=200, execution_status="SIMULATED",
            ))

    assert "competitive week" in str(excinfo.value)


def test_the_database_still_permits_the_non_execution_statuses_on_either_week():
    """MARKET_MOVED and friends are not executions and belong to both
    kinds of week. A backstop that refused them would break the ordinary
    "the market moved before we could act" path."""

    from app.db.models.competition import Ticket as TicketRow

    for rehearsal in (True, False):
        fix = Fixture()
        ticket_id = fix.ticket(rehearsal=rehearsal)
        with session_scope() as session:
            ticket = session.get(TicketRow, uuid.UUID(ticket_id))
            session.add(WagerRow(
                ticket_id=ticket.id, season_competitor_id=ticket.season_competitor_id,
                week_id=ticket.week_id, market_id=ticket.market_id,
                requested_stake_cents=200, execution_status="MARKET_MOVED",
            ))


# ======================================================================
# Settlement REFUSES corruption rather than reinterpreting it
# ======================================================================


def test_settling_a_placed_wager_whose_week_was_corrupted_to_rehearsal_refuses():
    """This replaces a test that accepted the opposite behaviour.

    It used to assert that such a settlement quietly became a simulated
    one: no credit, no event of the real kind, and a real $2 stake left
    debited forever. Suppressing the payout is safe about ADDING money and
    wrong about everything else -- it rewrites what happened to match the
    contradiction. Refusing leaves both the stake and the settlement
    exactly as they were, for a human to repair deliberately.
    """

    fix = Fixture()
    wager_id = fix.execute(fix.ticket(rehearsal=False), rehearsal=False)
    assert fix.balance() == Money.from_dollars_str("13.00")

    corrupt_profile(fix.competitive_week, is_real_money=False, counts_toward_standings=False, counts_toward_awards=False)

    with pytest.raises(RehearsalBoundaryViolation) as excinfo:
        fix.commissioner.settle_wager(
            wager_id=wager_id, result=SportsbookResult.WIN,
            payout=Money.from_dollars_str("3.74"),
        )

    assert "impossible state" in str(excinfo.value)
    # Nothing settled, nothing credited, nothing announced.
    from app.db.models.settlement import Settlement as SettlementRow

    with session_scope() as session:
        assert session.execute(
            select(SettlementRow).where(SettlementRow.wager_id == uuid.UUID(wager_id))
        ).scalars().all() == []
    assert fix.transactions("WIN_RETURN") == []
    assert fix.balance() == Money.from_dollars_str("13.00")
    assert "SIMULATED_SETTLED" not in fix.event_types(rehearsal=False)


def test_settling_a_simulated_wager_whose_week_was_corrupted_to_competitive_refuses():
    fix = Fixture()
    wager_id = _simulated_wager(fix)

    corrupt_profile(fix.rehearsal_week, is_real_money=True, counts_toward_standings=True, counts_toward_awards=True)

    with pytest.raises(RehearsalBoundaryViolation):
        fix.commissioner.settle_wager(
            wager_id=wager_id, result=SportsbookResult.WIN,
            payout=Money.from_dollars_str("3.74"),
        )

    assert fix.transactions("WIN_RETURN") == []
    assert fix.balance() == Money.from_dollars_str("15.00")


def test_the_commissioner_refuses_a_nonstandard_week_it_should_never_meet():
    """Defense in depth for legacy or restored data. The schema makes this
    unreachable now; the service is what is left if it ever is not."""

    fix = Fixture()
    corrupt_profile(fix.competitive_week, is_real_money=True, counts_toward_standings=True, counts_toward_awards=False)

    with pytest.raises(RehearsalBoundaryViolation) as excinfo:
        fix.ticket(rehearsal=False)
    assert "no reviewed profile" in str(excinfo.value)


def test_executing_against_a_nonstandard_week_is_refused():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)
    corrupt_profile(fix.competitive_week, is_real_money=True, counts_toward_standings=False, counts_toward_awards=True)

    with pytest.raises(RehearsalBoundaryViolation):
        fix.execute(ticket_id, rehearsal=False)

    assert fix.balance() == Money.from_dollars_str("15.00")


# ======================================================================
# The weekly decision is TERMINAL -- in both weeks, by the same rule
# ======================================================================
#
# `_has_weekly_decision` asked `has_placed_wager`, which matched
# `execution_status == "PLACED"` and nothing else. A rehearsal's official
# wager is SIMULATED, so after executing it the derived weekly-decision
# state read as "nothing decided yet": a second ticket could be issued, a
# second wager executed, and a PASS recorded after the bet. The rehearsal
# would have exercised a DIFFERENT state machine from the one competition
# uses -- which is the one thing a rehearsal must not do.
#
# The fix is NOT to make SIMULATED mean PLACED. The two stay distinct,
# because only one of them means money left the bankroll. What is shared
# is the concept above them: an EXECUTED wager.


def test_a_simulated_execution_spends_the_weekly_bet_decision():
    fix = Fixture()
    fix.execute(fix.ticket(rehearsal=True), rehearsal=True)

    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=True)


def test_a_pass_cannot_follow_a_simulated_execution():
    fix = Fixture()
    fix.execute(fix.ticket(rehearsal=True), rehearsal=True)

    with pytest.raises(DuplicateWeeklyDecision):
        fix.commissioner.record_pass(
            week_id=fix.rehearsal_week, season_competitor_id=fix.competitor,
            reason_for_pass="after the simulated bet",
        )


def test_a_second_already_issued_ticket_cannot_also_execute_as_simulated():
    """The narrower race: both tickets were issued BEFORE either executed,
    so the issuance guard never saw a decision. The execution guard is
    what has to catch this one."""

    fix = Fixture()
    first = fix.ticket(rehearsal=True)
    second = fix.ticket(rehearsal=True)
    fix.execute(first, rehearsal=True)

    with pytest.raises(DuplicateWeeklyDecision):
        fix.execute(second, rehearsal=True)

    with session_scope() as session:
        executed = session.execute(
            select(WagerRow).where(
                WagerRow.week_id == uuid.UUID(fix.rehearsal_week),
                WagerRow.execution_status.in_(("PLACED", "SIMULATED")),
            )
        ).scalars().all()
    assert len(executed) == 1


def test_a_ticket_cannot_follow_a_rehearsal_pass():
    fix = Fixture()
    fix.commissioner.record_pass(
        week_id=fix.rehearsal_week, season_competitor_id=fix.competitor,
        reason_for_pass="no edge",
    )

    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=True)


def test_competitive_terminal_decision_semantics_are_unchanged():
    fix = Fixture()
    fix.execute(fix.ticket(rehearsal=False), rehearsal=False)

    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=False)
    with pytest.raises(DuplicateWeeklyDecision):
        fix.commissioner.record_pass(
            week_id=fix.competitive_week, season_competitor_id=fix.competitor,
            reason_for_pass="after the real bet",
        )


@pytest.mark.parametrize(
    "status",
    [
        WagerExecutionStatus.MARKET_MOVED,
        WagerExecutionStatus.UNAVAILABLE,
        WagerExecutionStatus.MISSED_WINDOW,
        WagerExecutionStatus.SKIPPED,
    ],
)
def test_a_non_execution_does_not_spend_the_week(status):
    """A wager row exists, but the competitor never executed -- the market
    moved, the book pulled the line, the window closed. The week is not
    spent and a replacement decision is still allowed. Counting these as
    terminal merely because a row exists would strand a competitor with no
    decision for the week through no fault of its own."""

    fix = Fixture()
    fix.execute(
        fix.ticket(rehearsal=True), rehearsal=True, status=status,
        actual_stake=None, actual_line=None, actual_price=None,
    )

    replacement = fix.ticket(rehearsal=True)
    assert fix.commissioner.get_ticket(replacement).status.value == "ISSUED"


def test_the_terminal_decision_rule_reads_the_enum_rather_than_a_second_list():
    """`EXECUTED_STATUSES` is DERIVED from `.executes`. A second
    hand-maintained list is exactly how `has_placed_wager` came to mean
    PLACED while the code around it had learned about SIMULATED."""

    from app.domain.enums import EXECUTED_STATUSES, WagerExecutionStatus as W

    assert EXECUTED_STATUSES == {"PLACED", "SIMULATED"}
    assert EXECUTED_STATUSES == {s.value for s in W if s.executes}
    assert not any(W(s).executes is False for s in EXECUTED_STATUSES)


def test_the_full_rehearsal_bet_lifecycle():
    """End to end, in order: the state machine competition will run, with
    every financial effect removed and nothing else changed."""

    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=True)
    wager_id = fix.execute(ticket_id, rehearsal=True)

    assert fix.commissioner.get_ticket(ticket_id).status.value == "EXECUTED"
    with session_scope() as session:
        assert len(session.execute(
            select(WagerRow).where(WagerRow.week_id == uuid.UUID(fix.rehearsal_week))
        ).scalars().all()) == 1

    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=True)
    with pytest.raises(DuplicateWeeklyDecision):
        fix.commissioner.record_pass(
            week_id=fix.rehearsal_week, season_competitor_id=fix.competitor,
            reason_for_pass="x",
        )

    settlement_id = fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )
    assert settlement_id
    with pytest.raises(DuplicateSettlement):
        fix.commissioner.settle_wager(
            wager_id=wager_id, result=SportsbookResult.WIN,
            payout=Money.from_dollars_str("3.74"),
        )

    assert fix.transactions() == fix.transactions("SEASON_START")
    assert fix.balance() == Money.from_dollars_str("15.00")
    assert fix.commissioner.competitor_status(fix.competitor) is CompetitorStatus.ACTIVE
    assert fix.event_types(rehearsal=True) == {
        "WEEK_OPENED", "SIMULATED_TICKET_LOCKED", "SIMULATED_BET_EXECUTED",
        "SIMULATED_SETTLED",
    }


def test_the_full_competitive_bet_lifecycle_is_the_same_shape():
    fix = Fixture()
    ticket_id = fix.ticket(rehearsal=False)
    wager_id = fix.execute(ticket_id, rehearsal=False)

    assert fix.commissioner.get_ticket(ticket_id).status.value == "EXECUTED"
    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=False)

    fix.commissioner.settle_wager(
        wager_id=wager_id, result=SportsbookResult.WIN,
        payout=Money.from_dollars_str("3.74"),
    )
    assert fix.balance() == Money.from_dollars_str("16.74")
    assert fix.event_types(rehearsal=False) == {
        "WEEK_OPENED", "TICKET_LOCKED", "BET_EXECUTED", "PROP_WON",
        "BANKROLL_CHANGED",
    }


def test_the_full_rehearsal_pass_lifecycle():
    fix = Fixture()
    fix.commissioner.record_pass(
        week_id=fix.rehearsal_week, season_competitor_id=fix.competitor,
        reason_for_pass="no edge anywhere on the slate",
    )

    with pytest.raises(DuplicateWeeklyDecision):
        fix.ticket(rehearsal=True)
    assert fix.balance() == Money.from_dollars_str("15.00")
    assert fix.event_types(rehearsal=True) == {"WEEK_OPENED", "PASS_DECLARED"}
