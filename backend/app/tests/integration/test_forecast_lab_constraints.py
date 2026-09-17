"""DB-level defense in depth for research data (probability/confidence
bounds), and the cohort integrity check that catches two competitors'
"standardized" observations pointing at different evidence snapshots.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.forecast_lab import EvidenceSnapshot
from app.db.repositories.competitor_repository import CompetitorRepository
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.enums import Uncertainty
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.forecast_lab.cohort import evaluate_common_cohort
from app.forecast_lab.evidence_service import create_evidence_snapshot, mock_evidence_payload
from app.forecast_lab.forecast_service import create_forecast_observation
from app.forecast_lab.research_settlement_service import lock_research_settlement

WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}


def _setup_market():
    """Not tied to a Commissioner - these tests only exercise forecast_lab
    persistence directly. `games.season_id` is still a real FK, though, so
    a bare Season row (no rules) is created just to satisfy it."""

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        season = SeasonRepository(session).create_season(name=f"Constraint Test {uuid.uuid4()}", year=2026)
        repo = MarketRepository(session)
        game = repo.create_game(
            external_ref=f"g-{uuid.uuid4()}", season_id=season.id, week_number=1,
            home_team="KC", away_team="BUF", kickoff_at=now + timedelta(hours=100),
        )
        player = repo.create_player(external_ref=f"p-{uuid.uuid4()}", name="Player X", team="KC", position="WR")
        repo.create_game_player(game_id=game.id, player_id=player.id, team=game.home_team_canonical)
        market = repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="passing_yards")
        repo.add_quote(market_id=market.id, sportsbook="DRAFTKINGS", line=Decimal("225.5"), over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1))
        season_id, game_id, market_id = season.id, game.id, market.id

    with session_scope() as session:
        game_row = MarketRepository(session).get_game(game_id)
        run = capture_checkpoint(session, game=game_row, checkpoint_type="OPENING", windows_config=WINDOWS, now=now, canonical_sportsbook="DRAFTKINGS")
        assert run.status == "CAPTURED"

    with session_scope() as session:
        snapshot = MarketRepository(session).latest_snapshot(market_id)
        snapshot_id = snapshot.id
        evidence_id = session.execute(select(EvidenceSnapshot.id).where(EvidenceSnapshot.market_id == market_id)).scalar_one()

    return season_id, market_id, snapshot_id, evidence_id, now


def _make_season_competitor(season_id, competitor_id: str) -> uuid.UUID:
    with session_scope() as session:
        competitor_repo = CompetitorRepository(session)
        competitor_repo.ensure_identity(competitor_id=competitor_id, provider=competitor_id, display_name=competitor_id)
        row = competitor_repo.register_season_competitor(
            season_id=season_id, competitor_id=competitor_id, model_identifier="placeholder", model_version="v1"
        )
        return row.id


def test_model_probability_out_of_range_rejected_by_database():
    season_id, market_id, snapshot_id, evidence_id, now = _setup_market()
    sc_id = _make_season_competitor(season_id, "openai")
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("1.5"),  # invalid
                evidence_snapshot_id=evidence_id, confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )


def test_confidence_out_of_range_rejected_by_database():
    season_id, market_id, snapshot_id, evidence_id, now = _setup_market()
    sc_id = _make_season_competitor(season_id, "openai")
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("0.6"),
                evidence_snapshot_id=evidence_id, confidence=Decimal("11.0"), uncertainty=Uncertainty.MEDIUM,  # invalid
            )


def test_negative_probability_rejected_by_database():
    season_id, market_id, snapshot_id, evidence_id, now = _setup_market()
    sc_id = _make_season_competitor(season_id, "openai")
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("-0.1"),  # invalid
                evidence_snapshot_id=evidence_id, confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )


def test_cohort_flags_mismatched_evidence_snapshots_as_a_system_error():
    """If GPT and Claude's "OPENING" observations for the same market
    somehow point at different evidence snapshots, they were not actually
    compared under the same information - atomic checkpoint capture
    (ARCHITECTURE.md §4) guarantees this can't happen through the normal
    path, so this simulates it directly to prove the cohort code treats
    it as an exclusion rather than silently scoring it.
    """

    season_id, market_id, snapshot_id, evidence_id, now = _setup_market()
    gpt_id = _make_season_competitor(season_id, "openai")
    claude_id = _make_season_competitor(season_id, "anthropic")
    gemini_id = _make_season_competitor(season_id, "google")
    labels = {gpt_id: "GPT", claude_id: "CLAUDE", gemini_id: "GEMINI"}

    with session_scope() as session:
        snapshot = MarketRepository(session).get_market_snapshot(snapshot_id)

        # GPT and Gemini see the real evidence snapshot for this checkpoint...
        for sc_id in (gpt_id, gemini_id):
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("0.6"),
                evidence_snapshot_id=evidence_id, confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )

        # ...but Claude's observation is fabricated to point at a second,
        # independently-created evidence snapshot for the same market -
        # simulating whatever bug could cause this in a real pipeline.
        rogue_evidence = create_evidence_snapshot(
            session, market_id=market_id, market_snapshot_id=snapshot_id, generated_at=now,
            payload=mock_evidence_payload(generated_at=now, market_snapshot=snapshot),
            checkpoint_type="OPENING",
        )
        create_forecast_observation(
            session, season_competitor_id=claude_id, market_id=market_id, source_type="BENCHMARK",
            checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("0.55"),
            evidence_snapshot_id=rogue_evidence.id, confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
        )

    with session_scope() as session:
        report = evaluate_common_cohort(session, market_ids=[market_id], checkpoint_type="OPENING", competitor_labels=labels)

    assert report.eligible_count == 0
    assert report.rows[0].eligible is False
    assert report.rows[0].exclusion_reason == "OTHER"


def test_cohort_accepts_when_all_three_share_the_same_evidence_snapshot():
    season_id, market_id, snapshot_id, evidence_id, now = _setup_market()
    gpt_id = _make_season_competitor(season_id, "openai")
    claude_id = _make_season_competitor(season_id, "anthropic")
    gemini_id = _make_season_competitor(season_id, "google")
    labels = {gpt_id: "GPT", claude_id: "CLAUDE", gemini_id: "GEMINI"}

    with session_scope() as session:
        for sc_id in (gpt_id, claude_id, gemini_id):
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("0.6"),
                evidence_snapshot_id=evidence_id, confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )
        lock_research_settlement(session, market_id=market_id, stat_value=Decimal("230"), locked_at=now + timedelta(days=10))

    with session_scope() as session:
        report = evaluate_common_cohort(session, market_ids=[market_id], checkpoint_type="OPENING", competitor_labels=labels)

    assert report.eligible_count == 1
    assert report.rows[0].eligible is True


def test_create_forecast_observation_rejects_an_evidence_snapshot_for_a_different_market():
    """create_forecast_observation must not trust a caller-supplied
    MarketSnapshot alongside evidence_snapshot_id - it derives the market
    snapshot from the evidence row itself, so passing an evidence snapshot
    that actually belongs to a *different* market must be rejected rather
    than silently stamping the observation with mismatched data.
    """

    season_id_a, market_id_a, _snapshot_id_a, evidence_id_a, now = _setup_market()
    sc_id = _make_season_competitor(season_id_a, "openai")

    # A second, unrelated market/evidence snapshot.
    _season_id_b, market_id_b, _snapshot_id_b, _evidence_id_b, _now_b = _setup_market()
    assert market_id_a != market_id_b

    with pytest.raises(ValueError):
        with session_scope() as session:
            create_forecast_observation(
                session, season_competitor_id=sc_id, market_id=market_id_b, source_type="BENCHMARK",
                checkpoint_type="OPENING", timestamp=now, model_probability_over=Decimal("0.6"),
                evidence_snapshot_id=evidence_id_a,  # belongs to market_id_a, not market_id_b
                confidence=Decimal("7.0"), uncertainty=Uncertainty.MEDIUM,
            )

    # Nothing should have been written.
    with session_scope() as session:
        from app.db.models.forecast_lab import ForecastObservation

        assert session.execute(select(ForecastObservation).where(ForecastObservation.market_id == market_id_b)).first() is None


# --- Phase 4A.4 freshness guards, enforced by Postgres ------------------
#
# A CHECK nothing tests is decoration. These assert the database itself
# refuses the two shapes the application also refuses, so a future writer
# that bypasses MarketSnapshotService cannot land them.


def _bare_snapshot(market_id, **overrides):
    from app.db.models.markets import MarketSnapshot

    fields = dict(
        market_id=market_id,
        taken_at=datetime(2026, 9, 17, 20, tzinfo=timezone.utc),
        canonical_sportsbook="DRAFTKINGS",
        devig_method="PROPORTIONAL_V1",
        number_of_books=3,
        books_observed=3,
        stale_books_excluded=0,
        canonical_quote_stale=False,
        is_valid_canonical_baseline=False,
    )
    fields.update(overrides)
    return MarketSnapshot(**fields)


def test_a_negative_tolerance_is_refused_by_the_database():
    """Not a strict rule -- a configuration error. It marks every
    observation stale, so a run reads as a total market outage."""

    _, market_id, _, _, _ = _setup_market()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(_bare_snapshot(market_id, max_observation_age_seconds=-1))


def test_a_book_cannot_fall_out_of_both_coverage_counts():
    """books_observed must equal number_of_books + stale_books_excluded.
    Otherwise a book silently dropped from the feed and a book refused as
    stale both just shrink number_of_books, and degraded coverage becomes
    indistinguishable from thin coverage."""

    _, market_id, _, _, _ = _setup_market()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                _bare_snapshot(market_id, number_of_books=3, books_observed=5, stale_books_excluded=0)
            )


def test_consistent_coverage_counts_are_accepted():
    _, market_id, _, _, _ = _setup_market()
    with session_scope() as session:
        session.add(
            _bare_snapshot(
                market_id,
                number_of_books=3,
                books_observed=5,
                stale_books_excluded=2,
                max_observation_age_seconds=900,
            )
        )
