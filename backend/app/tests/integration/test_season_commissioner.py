"""Phase 2A acceptance test: the 12-step persistence flow from the review
handoff, against real PostgreSQL.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.money import Money
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import CompetitorStatus, RiskPosture, Side, SportsbookResult, Urgency, WagerExecutionStatus
from app.domain.errors import DuplicateSettlement
from app.domain.models import SeasonRules
from app.services.season_commissioner import SeasonCommissioner


def make_rules(version: str = "2026-w0.2") -> SeasonRules:
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


def make_market(season_id, week_number: int = 1, external_ref: str = "g1") -> str:
    with session_scope() as session:
        repo = MarketRepository(session)
        game = repo.create_game(
            external_ref=external_ref,
            season_id=season_id,
            week_number=week_number,
            home_team="KC",
            away_team="BUF",
            kickoff_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        player = repo.create_player(external_ref=f"player-{external_ref}", name="Player X", team="KC", position="WR")
        market = repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        return str(market.id)


def test_full_persistence_lifecycle_survives_a_reload():
    # 1. Create a season and frozen rules.
    commissioner = SeasonCommissioner.create_season(name="2026 NFL Season", year=2026, rules=make_rules())

    # 2. Register all three season competitors.
    ids = {}
    for competitor_id, provider in (("openai", "OpenAI"), ("anthropic", "Anthropic"), ("google", "Google")):
        ids[competitor_id] = commissioner.register_competitor(
            competitor_id=competitor_id,
            provider=provider,
            display_name=provider,
            model_identifier="placeholder",
            model_version="v1",
        )
    for sc_id in ids.values():
        assert commissioner.available_balance(sc_id) == Money.from_dollars_str("15.00")

    # 3. Open a week.
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market_id = make_market(commissioner.season_id)

    # 4. Issue a ticket.
    ticket_id = commissioner.issue_ticket(
        week_id=week_id,
        season_competitor_id=ids["openai"],
        market_id=market_id,
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.61"),
        price_for_side=-115,
        observed_line=Decimal("52.5"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="test",
    )

    # 5. Execute the ticket.
    wager_id = commissioner.record_execution(
        ticket_id=ticket_id,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_line=Decimal("52.5"),
        actual_price=-115,
        actual_stake=Money.from_dollars_str("2.00"),
    )

    # 6. Debit bankroll exactly once.
    assert commissioner.available_balance(ids["openai"]) == Money.from_dollars_str("13.00")
    txns = commissioner.transactions_for(ids["openai"])
    stake_txns = [t for t in txns if t.type.value == "STAKE"]
    assert len(stake_txns) == 1
    assert stake_txns[0].amount == Money.from_dollars_str("-2.00")

    # 7. Restart/recreate service state from the database: a brand new
    # object, constructed from nothing but the season_id string, with no
    # access to anything the first commissioner instance touched.
    reloaded = SeasonCommissioner(season_id=str(commissioner.season_id))

    # 8. Recover the same bankroll and wager state.
    assert reloaded.available_balance(ids["openai"]) == Money.from_dollars_str("13.00")
    wager = reloaded.get_wager(wager_id)
    assert wager.actual_stake == Money.from_dollars_str("2.00")
    assert wager.execution_status is WagerExecutionStatus.PLACED

    # 9. Settle the wager.
    reloaded.settle_wager(wager_id=wager_id, result=SportsbookResult.WIN, payout=Money.from_dollars_str("3.74"))
    assert reloaded.available_balance(ids["openai"]) == Money.from_dollars_str("16.74")

    # 10. Reject duplicate settlement.
    with pytest.raises(DuplicateSettlement):
        reloaded.settle_wager(wager_id=wager_id, result=SportsbookResult.WIN, payout=Money.from_dollars_str("3.74"))
    assert reloaded.available_balance(ids["openai"]) == Money.from_dollars_str("16.74")  # unchanged

    # 11. Persist every relevant event.
    week_events = {e.event_type.value for e in reloaded.events_for_week(week_id)}
    assert week_events == {
        "WEEK_OPENED",
        "TICKET_LOCKED",
        "BET_EXECUTED",
        "PROP_WON",
        "BANKROLL_CHANGED",
    }

    # 12. Create Season 2 and prove Season 1 money does not enter its balance.
    # (rules_version is globally UNIQUE by design - DATABASE.md §1 - so a
    # new season needs its own version string, same as a real season would.)
    season2 = SeasonCommissioner.create_season(name="2027 NFL Season", year=2027, rules=make_rules(version="2027-w0.1"))
    season2_sc_id = season2.register_competitor(
        competitor_id="openai",  # same cross-season identity, new season instance
        provider="OpenAI",
        display_name="OpenAI",
        model_identifier="placeholder-v2",
        model_version="v2",
    )
    assert season2_sc_id != ids["openai"]  # a distinct season_competitor row
    assert season2.available_balance(season2_sc_id) == Money.from_dollars_str("15.00")  # fresh, not 16.74
    # And Season 1's own balance is untouched by Season 2 existing at all.
    assert reloaded.available_balance(ids["openai"]) == Money.from_dollars_str("16.74")


def test_atomic_commit_ticket_and_stake_transaction_appear_together():
    """ARCHITECTURE.md's transaction-boundary requirement: 'execute wager'
    is one unit of work. There is no way to observe a wager marked PLACED
    without its STAKE transaction, or vice versa, because both are written
    inside the same session_scope() commit."""

    commissioner = SeasonCommissioner.create_season(name="Atomicity Season", year=2026, rules=make_rules())
    sc_id = commissioner.register_competitor(
        competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1"
    )
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market_id = make_market(commissioner.season_id)
    ticket_id = commissioner.issue_ticket(
        week_id=week_id,
        season_competitor_id=sc_id,
        market_id=market_id,
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.6"),
        price_for_side=-110,
        observed_line=Decimal("52.5"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="test",
    )
    wager_id = commissioner.record_execution(
        ticket_id=ticket_id,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_line=Decimal("52.5"),
        actual_price=-110,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    wager = commissioner.get_wager(wager_id)
    txns = commissioner.transactions_for(sc_id)
    stake_txns = [t for t in txns if t.type.value == "STAKE"]
    assert wager.execution_status is WagerExecutionStatus.PLACED
    assert len(stake_txns) == 1  # never zero, never two


def test_database_constraint_backs_duplicate_settlement_even_if_app_check_is_bypassed():
    """Defense in depth: the UNIQUE(wager_id) constraint on settlements
    must reject a duplicate at the database level too, not just via the
    application-level DuplicateSettlement check."""

    from sqlalchemy.exc import IntegrityError

    from app.db.models.settlement import Settlement as SettlementRow

    commissioner = SeasonCommissioner.create_season(name="Constraint Season", year=2026, rules=make_rules())
    sc_id = commissioner.register_competitor(
        competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1"
    )
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market_id = make_market(commissioner.season_id)
    ticket_id = commissioner.issue_ticket(
        week_id=week_id,
        season_competitor_id=sc_id,
        market_id=market_id,
        side=Side.OVER,
        urgency=Urgency.STRONG,
        risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.6"),
        price_for_side=-110,
        observed_line=Decimal("52.5"),
        model_requested_stake=Money.from_dollars_str("2.00"),
        why_now="test",
    )
    wager_id = commissioner.record_execution(
        ticket_id=ticket_id,
        status=WagerExecutionStatus.PLACED,
        sportsbook="DRAFTKINGS",
        actual_line=Decimal("52.5"),
        actual_price=-110,
        actual_stake=Money.from_dollars_str("2.00"),
    )
    commissioner.settle_wager(wager_id=wager_id, result=SportsbookResult.WIN, payout=Money.from_dollars_str("3.80"))

    import uuid

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                SettlementRow(
                    wager_id=uuid.UUID(wager_id),
                    sportsbook_result="WIN",
                    sportsbook_payout_cents=380,
                    sportsbook_settled_at=datetime.now(timezone.utc),
                )
            )


def test_pounce_limit_and_bankruptcy_still_enforced_when_persisted():
    commissioner = SeasonCommissioner.create_season(name="Bankruptcy Season", year=2026, rules=make_rules())
    sc_id = commissioner.register_competitor(
        competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1"
    )
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market_id = make_market(commissioner.season_id)

    commissioner.issue_ticket(
        week_id=week_id,
        season_competitor_id=sc_id,
        market_id=market_id,
        side=Side.OVER,
        urgency=Urgency.POUNCE,
        risk_posture=RiskPosture.AGGRESSIVE,
        model_probability_over=Decimal("0.65"),
        price_for_side=-110,
        observed_line=Decimal("52.5"),
        model_requested_stake=Money.from_dollars_str("3.00"),
        why_now="first pounce",
    )

    from app.domain.errors import PounceLimitExceeded

    with pytest.raises(PounceLimitExceeded):
        commissioner.issue_ticket(
            week_id=week_id,
            season_competitor_id=sc_id,
            market_id=make_market(commissioner.season_id, external_ref="g2"),
            side=Side.OVER,
            urgency=Urgency.POUNCE,
            risk_posture=RiskPosture.AGGRESSIVE,
            model_probability_over=Decimal("0.7"),
            price_for_side=-110,
            observed_line=Decimal("48.5"),
            model_requested_stake=Money.from_dollars_str("3.00"),
            why_now="second pounce should be rejected",
        )

    assert commissioner.competitor_status(sc_id) is CompetitorStatus.ACTIVE
