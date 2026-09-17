"""Concurrency safety: two simultaneous requests must not both succeed at
placing an official weekly decision for the same competitor, and must not
both squeeze past the Pounce limit. Without `_lock_competitor_week`'s
Postgres advisory transaction lock, these are ordinary SELECTs under
READ COMMITTED — two concurrent transactions can both observe "no
decision yet" before either commits. These tests run several racing
iterations because a race that isn't actually prevented is often not
guaranteed to manifest on the first try; the fix should make the outcome
deterministic regardless of timing, not just usually correct.
"""

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.money import Money
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import RiskPosture, Side, Urgency, WagerExecutionStatus
from app.domain.errors import DomainError
from app.domain.models import SeasonRules
from app.services.season_commissioner import SeasonCommissioner


def make_rules(version: str) -> SeasonRules:
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


def make_market(season_id, external_ref: str) -> str:
    with session_scope() as session:
        repo = MarketRepository(session)
        game = repo.create_game(
            external_ref=external_ref, season_id=season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        player = repo.create_player(external_ref=f"player-{external_ref}", name="Player X", team="KC", position="WR")
        repo.create_game_player(game_id=game.id, player_id=player.id, team=game.home_team_canonical)
        market = repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        return str(market.id)


def test_two_simultaneous_placements_never_both_succeed():
    for iteration in range(10):
        commissioner = SeasonCommissioner.create_season(name=f"Race {iteration}", year=2026, rules=make_rules(f"2026-race-{iteration}"))
        sc_id = commissioner.register_competitor(
            competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1"
        )
        week_id = commissioner.open_week(week_number=1, is_real_money=True)
        market_a = make_market(commissioner.season_id, f"race-{iteration}-a")
        market_b = make_market(commissioner.season_id, f"race-{iteration}-b")

        ticket_a = commissioner.issue_ticket(
            week_id=week_id, season_competitor_id=sc_id, market_id=market_a, side=Side.OVER, urgency=Urgency.STRONG,
            risk_posture=RiskPosture.STANDARD, model_probability_over=Decimal("0.6"), price_for_side=-110,
            observed_line=Decimal("52.5"), model_requested_stake=Money.from_dollars_str("2.00"), why_now="race a",
        )
        ticket_b = commissioner.issue_ticket(
            week_id=week_id, season_competitor_id=sc_id, market_id=market_b, side=Side.OVER, urgency=Urgency.STRONG,
            risk_posture=RiskPosture.STANDARD, model_probability_over=Decimal("0.6"), price_for_side=-110,
            observed_line=Decimal("48.5"), model_requested_stake=Money.from_dollars_str("2.00"), why_now="race b",
        )

        barrier = threading.Barrier(2)
        results = {}

        def place(name: str, ticket_id: str) -> None:
            barrier.wait()  # both threads start their transaction as close together as possible
            try:
                commissioner.record_execution(
                    ticket_id=ticket_id, status=WagerExecutionStatus.PLACED,
                    actual_line=Decimal("52.5"), actual_price=-110, actual_stake=Money.from_dollars_str("2.00"),
                )
                results[name] = "PLACED"
            except DomainError as exc:
                results[name] = type(exc).__name__

        t1 = threading.Thread(target=place, args=("a", ticket_a))
        t2 = threading.Thread(target=place, args=("b", ticket_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes = sorted(results.values())
        assert outcomes == ["DuplicateWeeklyDecision", "PLACED"], f"iteration {iteration}: {results}"

        # And the ledger only ever got debited once, regardless of the race.
        stake_txns = [t for t in commissioner.transactions_for(sc_id) if t.type.value == "STAKE"]
        assert len(stake_txns) == 1, f"iteration {iteration}: {stake_txns}"


def test_two_simultaneous_pounces_never_both_succeed():
    for iteration in range(10):
        commissioner = SeasonCommissioner.create_season(
            name=f"Pounce Race {iteration}", year=2026, rules=make_rules(f"2026-pounce-race-{iteration}")
        )
        sc_id = commissioner.register_competitor(
            competitor_id="openai", provider="OpenAI", display_name="OpenAI", model_identifier="x", model_version="v1"
        )
        week_id = commissioner.open_week(week_number=1, is_real_money=True)
        market_a = make_market(commissioner.season_id, f"pounce-race-{iteration}-a")
        market_b = make_market(commissioner.season_id, f"pounce-race-{iteration}-b")

        barrier = threading.Barrier(2)
        results = {}

        def pounce(name: str, market_id: str) -> None:
            barrier.wait()
            try:
                commissioner.issue_ticket(
                    week_id=week_id, season_competitor_id=sc_id, market_id=market_id, side=Side.OVER, urgency=Urgency.POUNCE,
                    risk_posture=RiskPosture.AGGRESSIVE, model_probability_over=Decimal("0.65"), price_for_side=-110,
                    observed_line=Decimal("52.5"), model_requested_stake=Money.from_dollars_str("3.00"), why_now="pounce race",
                )
                results[name] = "ISSUED"
            except DomainError as exc:
                results[name] = type(exc).__name__

        t1 = threading.Thread(target=pounce, args=("a", market_a))
        t2 = threading.Thread(target=pounce, args=("b", market_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes = sorted(results.values())
        assert outcomes == ["ISSUED", "PounceLimitExceeded"], f"iteration {iteration}: {results}"
