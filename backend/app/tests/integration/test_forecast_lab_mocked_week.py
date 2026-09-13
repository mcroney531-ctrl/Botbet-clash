"""Phase 2B acceptance test: one entirely mocked research week, end to
end, against real PostgreSQL — the 20-step flow from the Phase 2 review
handoff. No real AI provider, no real odds/stats provider: everything
here is either schedule-only allocation or caller-supplied fixture data.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.repositories.forecast_repository import ForecastRepository
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.research_repository import ResearchRepository
from app.db.session import session_scope
from app.domain.enums import Uncertainty
from app.domain.models import SeasonRules
from app.forecast_lab.benchmark_slate_service import commit_benchmark_slate_plan, resolve_benchmark_slots_for_game
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.forecast_lab.cohort import evaluate_common_cohort
from app.forecast_lab.forecast_service import create_forecast_observation
from app.forecast_lab.research_settlement_service import derive_outcome, lock_research_settlement
from app.forecast_lab.scoring import brier_score, log_loss
from app.core.money import Money
from app.services.season_commissioner import SeasonCommissioner

PROP_TYPES = ["passing_yards", "passing_touchdowns", "rushing_yards", "receptions", "receiving_yards"]
WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}
CANONICAL_BOOK = "DRAFTKINGS"


def make_rules() -> SeasonRules:
    return SeasonRules(
        rules_version="2026-mockweek",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


def test_mocked_research_week_survives_a_full_reload():
    # ---- 1-3. Season, rules, three season-competitors -----------------
    commissioner = SeasonCommissioner.create_season(name="Mock Research Week", year=2026, rules=make_rules())
    labels = {}
    for competitor_id, provider, label in (("openai", "OpenAI", "GPT"), ("anthropic", "Anthropic", "CLAUDE"), ("google", "Google", "GEMINI")):
        sc_id = commissioner.register_competitor(
            competitor_id=competitor_id, provider=provider, display_name=provider, model_identifier="placeholder", model_version="v1"
        )
        labels[uuid.UUID(sc_id)] = label
    gpt_id, claude_id, gemini_id = (
        next(k for k, v in labels.items() if v == "GPT"),
        next(k for k, v in labels.items() if v == "CLAUDE"),
        next(k for k, v in labels.items() if v == "GEMINI"),
    )

    # ---- 4. A mocked NFL week with multiple games ----------------------
    week_id = commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff_a = now + timedelta(hours=100)  # OPENING window [now-44h, now+4h] -> capturable now
    kickoff_d = now + timedelta(hours=105)  # OPENING window [now-39h, now+9h] -> capturable now
    kickoff_b = now + timedelta(hours=300)  # OPENING window far in the future -> stays PENDING

    with session_scope() as session:
        market_repo = MarketRepository(session)
        game_a = market_repo.create_game(
            external_ref="game-a", season_id=commissioner.season_id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=kickoff_a,
        )
        game_d = market_repo.create_game(
            external_ref="game-d", season_id=commissioner.season_id, week_number=1,
            home_team="SF", away_team="DAL", kickoff_at=kickoff_d,
        )
        game_b = market_repo.create_game(
            external_ref="game-b", season_id=commissioner.season_id, week_number=1,
            home_team="PHI", away_team="MIA", kickoff_at=kickoff_b,
        )

        player_a = market_repo.create_player(external_ref="player-a", name="Player A", team="KC", position="WR")
        player_a2 = market_repo.create_player(external_ref="player-a2", name="Player A2", team="KC", position="WR")
        player_d = market_repo.create_player(external_ref="player-d", name="Player D", team="SF", position="QB")

        # Game A: one clean, eligible market (the benchmark target) plus
        # one market that intentionally fails canonical-market eligibility
        # (step 11 - no DRAFTKINGS quote at all, only a non-canonical book).
        market_good = market_repo.create_prop_market(game_id=game_a.id, player_id=player_a.id, stat_type="passing_yards")
        market_repo.add_quote(market_id=market_good.id, sportsbook=CANONICAL_BOOK, line=Decimal("225.5"), over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1))
        market_repo.add_quote(market_id=market_good.id, sportsbook="FANDUEL", line=Decimal("225.5"), over_price=-110, under_price=-110, retrieved_at=now - timedelta(hours=1))

        market_no_canonical = market_repo.create_prop_market(game_id=game_a.id, player_id=player_a2.id, stat_type="receiving_yards")
        market_repo.add_quote(market_id=market_no_canonical.id, sportsbook="FANDUEL", line=Decimal("60.5"), over_price=-110, under_price=-110, retrieved_at=now - timedelta(hours=1))

        # Game D: single market, deliberately pushable (whole-number line)
        # -> ineligible, and it's the only market in the game, so its
        # assigned benchmark slot has nothing to fall back to (step 12).
        market_pushable = market_repo.create_prop_market(game_id=game_d.id, player_id=player_d.id, stat_type="passing_touchdowns")
        market_repo.add_quote(market_id=market_pushable.id, sportsbook=CANONICAL_BOOK, line=Decimal("2"), over_price=-120, under_price=100, retrieved_at=now - timedelta(hours=1))

        game_a_id, game_d_id, game_b_id = game_a.id, game_d.id, game_b.id
        market_good_id, market_no_canonical_id, market_pushable_id = market_good.id, market_no_canonical.id, market_pushable.id

    # ---- 5. Benchmark slot plan committed (schedule-only) ---------------
    with session_scope() as session:
        market_repo = MarketRepository(session)
        games = [market_repo.get_game(game_a_id), market_repo.get_game(game_d_id), market_repo.get_game(game_b_id)]
        plan = commit_benchmark_slate_plan(
            session, week_id=uuid.UUID(week_id), games=games, target_slot_count=3, prop_types=PROP_TYPES, committed_at=now
        )
        plan_id = plan.id

    # ---- 6. One game's (game_a's) OPENING checkpoint fires --------------
    with session_scope() as session:
        market_repo = MarketRepository(session)
        game_a_row = market_repo.get_game(game_a_id)
        run_a = capture_checkpoint(session, game=game_a_row, checkpoint_type="OPENING", windows_config=WINDOWS, now=now, canonical_sportsbook=CANONICAL_BOOK)
        assert run_a.status == "CAPTURED"

        game_d_row = market_repo.get_game(game_d_id)
        run_d = capture_checkpoint(session, game=game_d_row, checkpoint_type="OPENING", windows_config=WINDOWS, now=now, canonical_sportsbook=CANONICAL_BOOK)
        assert run_d.status == "CAPTURED"

        game_b_row = market_repo.get_game(game_b_id)
        run_b = capture_checkpoint(session, game=game_b_row, checkpoint_type="OPENING", windows_config=WINDOWS, now=now, canonical_sportsbook=CANONICAL_BOOK)
        assert run_b.status == "PENDING"  # window hasn't opened yet - proves per-game independence

        # Idempotency: capturing game_a again must be a pure no-op.
        run_a_again = capture_checkpoint(session, game=game_a_row, checkpoint_type="OPENING", windows_config=WINDOWS, now=now + timedelta(minutes=5), canonical_sportsbook=CANONICAL_BOOK)
        assert run_a_again.id == run_a.id
        assert run_a_again.captured_at == run_a.captured_at  # unchanged, not re-captured

    # ---- 7. Its qualifying slots resolve ---------------------------------
    with session_scope() as session:
        resolve_benchmark_slots_for_game(session, plan_id=plan_id, game_id=game_a_id, resolved_at=now)
        resolve_benchmark_slots_for_game(session, plan_id=plan_id, game_id=game_d_id, resolved_at=now)
        # game_b's slot is left untouched (PENDING) - its checkpoint never fired.

    with session_scope() as session:
        from app.db.repositories.benchmark_repository import BenchmarkRepository

        slots = {s.game_id: s for s in BenchmarkRepository(session).slots_for_plan(plan_id)}
        assert slots[game_a_id].status == "RESOLVED"
        assert slots[game_a_id].resolved_market_id == market_good_id  # not the canonical-unavailable one
        assert slots[game_d_id].status == "UNFILLABLE"  # step 12: no eligible market, nothing to fall back to
        assert slots[game_b_id].status == "PENDING"

    # ---- 8-9. Canonical/consensus snapshots frozen; identical evidence --
    with session_scope() as session:
        market_repo = MarketRepository(session)
        good_snapshot = market_repo.latest_snapshot(market_good_id)
        assert good_snapshot.is_valid_canonical_baseline is True
        assert good_snapshot.canonical_line == Decimal("225.5")
        assert good_snapshot.number_of_books == 2
        assert good_snapshot.same_line_consensus_over_probability is not None  # both books quote 225.5

        no_canonical_snapshot = market_repo.latest_snapshot(market_no_canonical_id)
        assert no_canonical_snapshot.is_valid_canonical_baseline is False  # step 11

        pushable_snapshot = market_repo.latest_snapshot(market_pushable_id)
        assert pushable_snapshot.is_valid_canonical_baseline is True
        assert pushable_snapshot.canonical_line == Decimal("2")  # whole number -> pushable

        good_snapshot_id, no_canonical_snapshot_id, pushable_snapshot_id = good_snapshot.id, no_canonical_snapshot.id, pushable_snapshot.id

        # Evidence snapshot: exactly one per market at this checkpoint -
        # every competitor's forecast will reference this same row.
        from app.db.models.forecast_lab import EvidenceSnapshot
        from sqlalchemy import select

        evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_good_id, EvidenceSnapshot.checkpoint_type == "OPENING")
        ).scalar_one()

    # ---- 10. Synthetic OPENING forecasts for all three, plus one on the
    # canonical-unavailable market (step 11's exclusion path) -----------
    opening_forecasts = {}
    with session_scope() as session:
        market_repo = MarketRepository(session)
        good_snapshot_row = market_repo.get_market_snapshot(good_snapshot_id)
        for sc_id, prob in ((gpt_id, Decimal("0.61")), (claude_id, Decimal("0.55")), (gemini_id, Decimal("0.58"))):
            obs = create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_good_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=prob,
                market_snapshot=good_snapshot_row, evidence_snapshot_id=evidence_id,
                confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )
            opening_forecasts[sc_id] = obs.id
            assert obs.research_eligible is True

        no_canonical_snapshot_row = market_repo.get_market_snapshot(no_canonical_snapshot_id)
        no_canonical_evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_no_canonical_id)
        ).scalar_one()
        for sc_id, prob in ((gpt_id, Decimal("0.50")), (claude_id, Decimal("0.52")), (gemini_id, Decimal("0.49"))):
            obs = create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_no_canonical_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=prob,
                market_snapshot=no_canonical_snapshot_row, evidence_snapshot_id=no_canonical_evidence_id,
                confidence=Decimal("5.0"), uncertainty=Uncertainty.HIGH,
            )
            assert obs.research_eligible is False
            assert obs.exclusion_reason == "CANONICAL_MARKET_UNAVAILABLE"

    # ---- 11-13. MID: the line moves, and forecasts are revised ----------
    # kickoff_a's MID window is [kickoff-60h, kickoff-36h] = [now+40h, now+64h].
    mid_time = kickoff_a - timedelta(hours=50)
    with session_scope() as session:
        market_repo = MarketRepository(session)
        # A fresh, later quote at a new line - this is what "the line moved" means.
        market_repo.add_quote(market_id=market_good_id, sportsbook=CANONICAL_BOOK, line=Decimal("230.5"), over_price=-110, under_price=-110, retrieved_at=mid_time)
        market_repo.add_quote(market_id=market_good_id, sportsbook="FANDUEL", line=Decimal("230.5"), over_price=-112, under_price=-108, retrieved_at=mid_time)
        game_a_row = market_repo.get_game(game_a_id)
        run_mid = capture_checkpoint(session, game=game_a_row, checkpoint_type="MID", windows_config=WINDOWS, now=mid_time, canonical_sportsbook=CANONICAL_BOOK)
        assert run_mid.status == "CAPTURED"
        mid_snapshot = market_repo.latest_snapshot(market_good_id)
        assert mid_snapshot.canonical_line == Decimal("230.5")  # moved from 225.5

        from sqlalchemy import select as _select

        from app.db.models.forecast_lab import EvidenceSnapshot as _EvidenceSnapshot

        mid_evidence_id = session.execute(
            _select(_EvidenceSnapshot.id).where(_EvidenceSnapshot.market_id == market_good_id, _EvidenceSnapshot.checkpoint_type == "MID")
        ).scalar_one()

        mid_obs = create_forecast_observation(
            session, season_competitor_id=gpt_id, market_id=market_good_id, source_type="BENCHMARK",
            checkpoint_type="MID", timestamp=mid_time, model_probability_over=Decimal("0.66"),
            market_snapshot=mid_snapshot, evidence_snapshot_id=mid_evidence_id,
            confidence=Decimal("7.5"), uncertainty=Uncertainty.MEDIUM,
            revision_parent_id=opening_forecasts[gpt_id], revision_reason="SCHEDULED_CHECKPOINT",
        )
        assert mid_obs.canonical_line == Decimal("230.5")

    # ---- 14. OPENING observations remain unchanged -----------------------
    with session_scope() as session:
        opening_gpt = ForecastRepository(session).get(opening_forecasts[gpt_id])
        assert opening_gpt.canonical_line == Decimal("225.5")  # untouched by the MID revision
        assert opening_gpt.model_probability_over == Decimal("0.61")
        assert opening_gpt.revision_parent_id is None
        mid_gpt = ForecastRepository(session).latest_for_competitor_market_checkpoint(gpt_id, market_good_id, "MID")
        assert mid_gpt.revision_parent_id == opening_gpt.id
        assert mid_gpt.id != opening_gpt.id  # a new row, never an UPDATE

    # ---- 15. Research final stats persisted ------------------------------
    # 228 straddles the two checkpoints' lines: OVER the Opening 225.5,
    # UNDER the Mid 230.5 - proving a single final stat resolves each
    # checkpoint's own forecast independently and can even disagree.
    with session_scope() as session:
        lock_research_settlement(session, market_id=market_good_id, stat_value=Decimal("228"), locked_at=now + timedelta(days=10))

    # ---- 16. Each checkpoint's result derived against its own line ------
    with session_scope() as session:
        settlement = ResearchRepository(session).get_for_market(market_good_id)
        stat_value = settlement.research_stat_value_at_lock
        opening_line = ForecastRepository(session).get(opening_forecasts[gpt_id]).canonical_line
        mid_line = ForecastRepository(session).latest_for_competitor_market_checkpoint(gpt_id, market_good_id, "MID").canonical_line
        opening_outcome = derive_outcome(stat_value, opening_line)
        mid_outcome = derive_outcome(stat_value, mid_line)
        assert opening_outcome == "OVER"
        assert mid_outcome == "UNDER"

    # ---- 17. Brier and log-loss for models and market at OPENING --------
    with session_scope() as session:
        forecast_repo = ForecastRepository(session)
        market_repo = MarketRepository(session)
        good_snapshot_row = market_repo.get_market_snapshot(good_snapshot_id)

        scores = {}
        for sc_id, label in labels.items():
            obs = forecast_repo.latest_for_competitor_market_checkpoint(sc_id, market_good_id, "OPENING")
            scores[label] = {
                "brier": brier_score(obs.model_probability_over, opening_outcome),
                "log_loss": log_loss(obs.model_probability_over, opening_outcome),
            }
        scores["CANONICAL_MARKET"] = {
            "brier": brier_score(good_snapshot_row.canonical_over_probability, opening_outcome),
            "log_loss": log_loss(good_snapshot_row.canonical_over_probability, opening_outcome),
        }
        scores["SAME_LINE_CONSENSUS"] = {
            "brier": brier_score(good_snapshot_row.same_line_consensus_over_probability, opening_outcome),
            "log_loss": log_loss(good_snapshot_row.same_line_consensus_over_probability, opening_outcome),
        }

    assert scores["GPT"]["brier"] == (Decimal("0.61") - 1) ** 2
    assert scores["CLAUDE"]["brier"] == (Decimal("0.55") - 1) ** 2
    assert scores["GEMINI"]["brier"] == (Decimal("0.58") - 1) ** 2
    # Higher probability on the side that actually won (OVER) -> lower Brier.
    assert scores["GPT"]["brier"] < scores["CLAUDE"]["brier"]

    # ---- 18. Common-cohort coverage excludes incomplete rows -------------
    with session_scope() as session:
        report = evaluate_common_cohort(
            session,
            market_ids=[market_good_id, market_no_canonical_id, market_pushable_id],
            checkpoint_type="OPENING",
            competitor_labels=labels,
        )
    assert report.potential_count == 3
    assert report.eligible_count == 1
    by_market = {row.market_id: row for row in report.rows}
    assert by_market[market_good_id].eligible is True
    assert by_market[market_no_canonical_id].eligible is False
    assert by_market[market_no_canonical_id].exclusion_reason == "CANONICAL_MARKET_UNAVAILABLE"
    assert by_market[market_pushable_id].eligible is False
    assert by_market[market_pushable_id].exclusion_reason == "GPT_FORECAST_MISSING"  # never forecast at all
    assert report.coverage_percentage == Decimal("33.3")

    # ---- 19-20. Reload from scratch; the whole timeline and every score
    # reproduce exactly - nothing above was cached anywhere but the DB. --
    reloaded_commissioner = SeasonCommissioner(season_id=str(commissioner.season_id))
    assert reloaded_commissioner.available_balance(str(gpt_id)) == Money.from_dollars_str("15.00")

    with session_scope() as fresh_session:
        fresh_forecast_repo = ForecastRepository(fresh_session)
        fresh_market_repo = MarketRepository(fresh_session)
        fresh_good_snapshot = fresh_market_repo.get_market_snapshot(good_snapshot_id)

        fresh_scores = {}
        for sc_id, label in labels.items():
            obs = fresh_forecast_repo.latest_for_competitor_market_checkpoint(sc_id, market_good_id, "OPENING")
            fresh_scores[label] = {
                "brier": brier_score(obs.model_probability_over, opening_outcome),
                "log_loss": log_loss(obs.model_probability_over, opening_outcome),
            }
        fresh_scores["CANONICAL_MARKET"] = {
            "brier": brier_score(fresh_good_snapshot.canonical_over_probability, opening_outcome),
            "log_loss": log_loss(fresh_good_snapshot.canonical_over_probability, opening_outcome),
        }
        fresh_scores["SAME_LINE_CONSENSUS"] = {
            "brier": brier_score(fresh_good_snapshot.same_line_consensus_over_probability, opening_outcome),
            "log_loss": log_loss(fresh_good_snapshot.same_line_consensus_over_probability, opening_outcome),
        }

        fresh_report = evaluate_common_cohort(
            fresh_session,
            market_ids=[market_good_id, market_no_canonical_id, market_pushable_id],
            checkpoint_type="OPENING",
            competitor_labels=labels,
        )

    assert fresh_scores == scores
    assert fresh_report.potential_count == report.potential_count
    assert fresh_report.eligible_count == report.eligible_count
    assert fresh_report.coverage_percentage == report.coverage_percentage
    assert {r.market_id: (r.eligible, r.exclusion_reason) for r in fresh_report.rows} == {
        r.market_id: (r.eligible, r.exclusion_reason) for r in report.rows
    }
