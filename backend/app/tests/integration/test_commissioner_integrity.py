"""Season-membership, week-lifecycle, and Pounce-expiry integrity — the
Phase 2 hardening pass items that were deliberately deferred to "once
persistence exists" and are now being enforced.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.money import Money
from app.db.models.competition import Ticket as TicketRow
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.enums import RiskPosture, Side, Urgency, WagerExecutionStatus
from app.domain.errors import (
    CrossSeasonReference,
    DuplicateWeeklyDecision,
    InvalidStateTransition,
    MarketNotInWeek,
    PounceLimitExceeded,
    WeekNotOpen,
)
from app.domain.models import SeasonRules
from app.services.season_commissioner import SeasonCommissioner


def make_rules(version="2026-integrity") -> SeasonRules:
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


def make_market(season_id, external_ref="g1", week_number=1) -> str:
    with session_scope() as session:
        repo = MarketRepository(session)
        game = repo.create_game(
            external_ref=external_ref, season_id=season_id, week_number=week_number,
            home_team="KC", away_team="BUF", kickoff_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        player = repo.create_player(external_ref=f"player-{external_ref}", name="Player X", team="KC", position="WR")
        market = repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        return str(market.id)


def issue(commissioner, *, week_id, season_competitor_id, market_id, **overrides):
    defaults = dict(
        week_id=week_id, season_competitor_id=season_competitor_id, market_id=market_id,
        side=Side.OVER, urgency=Urgency.STRONG, risk_posture=RiskPosture.STANDARD,
        model_probability_over=Decimal("0.6"), price_for_side=-110, observed_line=Decimal("52.5"),
        model_requested_stake=Money.from_dollars_str("2.00"), why_now="test",
    )
    defaults.update(overrides)
    return commissioner.issue_ticket(**defaults)


def test_cross_season_week_id_rejected_and_transaction_rolls_back():
    season1 = SeasonCommissioner.create_season(name="Season One", year=2026, rules=make_rules("2026-a"))
    sc1 = season1.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1 = season1.open_week(week_number=1, is_real_money=True)
    market1 = make_market(season1.season_id)

    season2 = SeasonCommissioner.create_season(name="Season Two", year=2027, rules=make_rules("2027-a"))
    week2 = season2.open_week(week_number=1, is_real_money=True)

    # season1's Commissioner handed season2's week_id.
    with pytest.raises(CrossSeasonReference):
        issue(season1, week_id=week2, season_competitor_id=sc1, market_id=market1)

    # Nothing was written - not even a partial ticket.
    with session_scope() as session:
        assert session.execute(select(TicketRow)).first() is None


def test_cross_season_competitor_id_rejected():
    season1 = SeasonCommissioner.create_season(name="Season One", year=2026, rules=make_rules("2026-b"))
    week1 = season1.open_week(week_number=1, is_real_money=True)
    market1 = make_market(season1.season_id)

    season2 = SeasonCommissioner.create_season(name="Season Two", year=2027, rules=make_rules("2027-b"))
    sc2 = season2.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")

    with pytest.raises(CrossSeasonReference):
        issue(season1, week_id=week1, season_competitor_id=sc2, market_id=market1)


def test_cross_season_market_id_rejected():
    season1 = SeasonCommissioner.create_season(name="Season One", year=2026, rules=make_rules("2026-c"))
    sc1 = season1.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1 = season1.open_week(week_number=1, is_real_money=True)

    season2 = SeasonCommissioner.create_season(name="Season Two", year=2027, rules=make_rules("2027-c"))
    market2 = make_market(season2.season_id)

    with pytest.raises(CrossSeasonReference):
        issue(season1, week_id=week1, season_competitor_id=sc1, market_id=market2)


def test_record_execution_rejects_ticket_from_a_foreign_season_commissioner():
    season1 = SeasonCommissioner.create_season(name="Season One", year=2026, rules=make_rules("2026-d"))
    sc1 = season1.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1 = season1.open_week(week_number=1, is_real_money=True)
    market1 = make_market(season1.season_id)
    ticket_id = issue(season1, week_id=week1, season_competitor_id=sc1, market_id=market1)

    season2 = SeasonCommissioner.create_season(name="Season Two", year=2027, rules=make_rules("2027-d"))
    with pytest.raises(CrossSeasonReference):
        season2.record_execution(
            ticket_id=ticket_id, status=WagerExecutionStatus.PLACED,
            actual_line=Decimal("52.5"), actual_price=-110, actual_stake=Money.from_dollars_str("2.00"),
        )


def test_issue_ticket_rejects_a_week_that_is_not_opened():
    commissioner = SeasonCommissioner.create_season(name="Not Opened", year=2026, rules=make_rules("2026-e"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    market_id = make_market(commissioner.season_id)
    week_id = commissioner.open_week(week_number=1, is_real_money=True)

    with session_scope() as session:
        SeasonRepository(session).close_week(uuid.UUID(week_id), closed_at=datetime.now(timezone.utc))

    with pytest.raises(WeekNotOpen):
        issue(commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market_id)


def test_record_pass_rejects_a_week_that_is_not_opened():
    commissioner = SeasonCommissioner.create_season(name="Not Opened Pass", year=2026, rules=make_rules("2026-f"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week_id = commissioner.open_week(week_number=1, is_real_money=True)

    with session_scope() as session:
        SeasonRepository(session).close_week(uuid.UUID(week_id), closed_at=datetime.now(timezone.utc))

    with pytest.raises(WeekNotOpen):
        commissioner.record_pass(week_id=week_id, season_competitor_id=sc_id, reason_for_pass="too late")


def test_close_week_rejects_when_a_game_is_not_yet_final():
    commissioner = SeasonCommissioner.create_season(name="Unfinished Games", year=2026, rules=make_rules("2026-g"))
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    with session_scope() as session:
        repo = MarketRepository(session)
        repo.create_game(
            external_ref="unfinished-game", season_id=commissioner.season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=datetime.now(timezone.utc) - timedelta(days=1), status="IN_PROGRESS",
        )

    with pytest.raises(InvalidStateTransition):
        commissioner.close_week(week_id)


def test_close_week_succeeds_once_all_games_are_final():
    commissioner = SeasonCommissioner.create_season(name="Finished Games", year=2026, rules=make_rules("2026-h"))
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    with session_scope() as session:
        repo = MarketRepository(session)
        repo.create_game(
            external_ref="finished-game", season_id=commissioner.season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=datetime.now(timezone.utc) - timedelta(days=1), status="FINAL",
        )

    commissioner.close_week(week_id)  # must not raise


def test_expired_pounce_does_not_block_a_replacement():
    commissioner = SeasonCommissioner.create_season(name="Pounce Expiry", year=2026, rules=make_rules("2026-i"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market1 = make_market(commissioner.season_id, external_ref="pounce-1")
    market2 = make_market(commissioner.season_id, external_ref="pounce-2")

    already_expired = datetime.now(timezone.utc) - timedelta(hours=1)
    issue(
        commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market1,
        urgency=Urgency.POUNCE, valid_until=already_expired,
    )

    # A second Pounce must be issuable - the first one already expired,
    # per RULES.md §74: "an expired, unexecuted Pounce may be replaced."
    second_ticket_id = issue(
        commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market2, urgency=Urgency.POUNCE,
    )
    assert second_ticket_id is not None


def test_unexpired_pounce_still_blocks_a_second_one():
    commissioner = SeasonCommissioner.create_season(name="Pounce Still Active", year=2026, rules=make_rules("2026-j"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market1 = make_market(commissioner.season_id, external_ref="pounce-3")
    market2 = make_market(commissioner.season_id, external_ref="pounce-4")

    not_yet_expired = datetime.now(timezone.utc) + timedelta(hours=1)
    issue(
        commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market1,
        urgency=Urgency.POUNCE, valid_until=not_yet_expired,
    )
    with pytest.raises(PounceLimitExceeded):
        issue(commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market2, urgency=Urgency.POUNCE)


def test_issue_ticket_rejects_a_market_from_a_different_week_same_season():
    commissioner = SeasonCommissioner.create_season(name="Wrong Week Market", year=2026, rules=make_rules("2026-k"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1_id = commissioner.open_week(week_number=1, is_real_money=True)
    # Same season, but this market's game is scheduled for week 12.
    week12_market = make_market(commissioner.season_id, external_ref="week12-market", week_number=12)

    with pytest.raises(MarketNotInWeek):
        issue(commissioner, week_id=week1_id, season_competitor_id=sc_id, market_id=week12_market)


def test_record_pass_rejects_a_candidate_market_from_a_different_week():
    commissioner = SeasonCommissioner.create_season(name="Wrong Week Candidate", year=2026, rules=make_rules("2026-l"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1_id = commissioner.open_week(week_number=1, is_real_money=True)
    week12_market = make_market(commissioner.season_id, external_ref="week12-candidate", week_number=12)

    with pytest.raises(MarketNotInWeek):
        commissioner.record_pass(
            week_id=week1_id, season_competitor_id=sc_id, reason_for_pass="best candidate wasn't good enough",
            best_available_candidate_market_id=week12_market,
        )


def test_record_pass_accepts_a_candidate_market_from_the_same_week():
    commissioner = SeasonCommissioner.create_season(name="Right Week Candidate", year=2026, rules=make_rules("2026-m"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week1_id = commissioner.open_week(week_number=1, is_real_money=True)
    week1_market = make_market(commissioner.season_id, external_ref="week1-candidate", week_number=1)

    decision_id = commissioner.record_pass(
        week_id=week1_id, season_competitor_id=sc_id, reason_for_pass="edge too small",
        best_available_candidate_market_id=week1_market,
    )
    assert decision_id is not None


def test_issue_ticket_rejects_a_new_ticket_after_bet_executed():
    commissioner = SeasonCommissioner.create_season(name="Terminal Bet", year=2026, rules=make_rules("2026-n"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market1 = make_market(commissioner.season_id, external_ref="terminal-bet-1")
    market2 = make_market(commissioner.season_id, external_ref="terminal-bet-2")

    ticket_id = issue(commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market1)
    commissioner.record_execution(
        ticket_id=ticket_id, status=WagerExecutionStatus.PLACED,
        actual_line=Decimal("52.5"), actual_price=-110, actual_stake=Money.from_dollars_str("2.00"),
    )

    # BET_EXECUTED is terminal - no further ticket should even be issuable,
    # not just unexecutable.
    with pytest.raises(DuplicateWeeklyDecision):
        issue(commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market2)


def test_issue_ticket_rejects_a_new_ticket_after_pass_locked():
    commissioner = SeasonCommissioner.create_season(name="Terminal Pass", year=2026, rules=make_rules("2026-o"))
    sc_id = commissioner.register_competitor(competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1")
    week_id = commissioner.open_week(week_number=1, is_real_money=True)
    market1 = make_market(commissioner.season_id, external_ref="terminal-pass-1")

    commissioner.record_pass(week_id=week_id, season_competitor_id=sc_id, reason_for_pass="no edge this week")

    with pytest.raises(DuplicateWeeklyDecision):
        issue(commissioner, week_id=week_id, season_competitor_id=sc_id, market_id=market1)
