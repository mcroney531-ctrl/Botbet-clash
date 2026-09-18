"""Phase 4A.4 — the refresh -> commit -> capture choreography, and the
seven scenarios that justify a tolerance.

Zero Odds API credits: the refresh is injected, so a FAILING refresh is
just a callable that returns `ok=False`. That is the whole reason the
cycle takes a `refresh` rather than constructing a provider.

The methodology point these scenarios exist to make: a successful refresh
immediately before capture produces near-zero observation ages, so
watching healthy captures tells you nothing about what the tolerance
should be. The tolerance is a POST-FAILURE grace period — how old we are
willing to let the last successful observation be when the refresh that
should have preceded this capture did not work. Only the failure cases
speak to that.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models.ingestion import ProviderCall
from app.db.models.markets import CheckpointRun, Game, MarketSnapshot, Player, PropMarket
from app.db.models.season import Season
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.forecast_lab.calibration import preview_thresholds, render as render_calibration
from app.marketdata.base import ProviderCallMetadata
from app.marketdata.checkpoint_cycle import (
    SINGLE_ATTEMPT,
    CapturePolicy,
    RefreshOutcome,
    RefreshRetryPolicy,
    render,
    run_checkpoint_cycle,
)
from app.marketdata.dto import ProviderEventRef, ProviderPlayerRef, ProviderQuote
from app.marketdata.ingestion import IngestionService
from app.marketdata.telemetry import record_call, start_run

PROVIDER = "THE_ODDS_API"
CANONICAL = "DRAFTKINGS"

KICKOFF = datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc)
# Inside FINAL (kickoff-6h .. kickoff-2h); target_time is kickoff-3h.
CAPTURE_AT = KICKOFF - timedelta(hours=3)
TARGET_TIME = KICKOFF - timedelta(hours=3)

WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}

# A candidate grace period to reason against. Not a recommendation and not
# a default anywhere in the code -- the scenarios below are what a
# recommendation has to be argued from.
CANDIDATE = 900


# --- fixtures ---------------------------------------------------------


def _game(tag: str) -> uuid.UUID:
    with session_scope() as session:
        season = Season(year=2026, name=f"cycle-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        game = Game(
            external_ref=f"cycle-{tag}",
            season_id=season.id,
            week_number=3,
            home_team="Buffalo Bills",
            away_team="Detroit Lions",
            home_team_canonical="BUF",
            away_team_canonical="DET",
            kickoff_at=KICKOFF,
        )
        session.add(game)
        session.flush()
        return game.id


def _market(game_id: uuid.UUID, tag: str) -> uuid.UUID:
    with session_scope() as session:
        player = Player(external_ref=f"GSIS:cycle-{tag}", name=f"Player {tag}")
        session.add(player)
        session.flush()
        market = PropMarket(game_id=game_id, player_id=player.id, stat_type="receiving_yards")
        session.add(market)
        session.flush()
        return market.id


def _call(session, tag: str) -> ProviderCall:
    """One provider call per simulated poll: the quote fingerprint is keyed
    on the call id, so reusing one would dedup the second observation of an
    unchanged price away and there would be nothing to age."""

    run = start_run(session, provider=PROVIDER, operation="FETCH_QUOTES")
    return record_call(
        session,
        run=run,
        metadata=ProviderCallMetadata(
            endpoint_capability="FETCH_EVENT_ODDS",
            requested_at=CAPTURE_AT,
            responded_at=CAPTURE_AT,
            http_status=200,
            raw_response_body=f"json-{tag}".encode(),
            raw_response_sha256="d" * 64,
            raw_response_bytes=12,
        ),
        success=True,
    )


def _write_quote(market_id: uuid.UUID, *, sportsbook: str, as_of_at: datetime, line="74.5", tag=None):
    with session_scope() as session:
        call = _call(session, tag or f"{sportsbook}-{as_of_at.isoformat()}")
        row = IngestionService(session).persist_quote(
            quote=ProviderQuote(
                event=ProviderEventRef(provider=PROVIDER, external_event_id=f"e-{market_id}"),
                player=ProviderPlayerRef(provider=PROVIDER, display_name="Player X"),
                stat_family=StatFamily.RECEIVING_YARDS,
                vendor_market_key="player_reception_yds",
                sportsbook=sportsbook,
                line=Decimal(line),
                over_price=-165,
                under_price=129,
                as_of_at=as_of_at,
                retrieved_at=as_of_at,
                source=PROVIDER,
            ),
            market_id=market_id,
            provider_call=call,
        )
        assert row is not None, "fixture wrote a duplicate fingerprint"


def _policy(tolerance=CANDIDATE, retry=SINGLE_ATTEMPT, rules_version=None) -> CapturePolicy:
    return CapturePolicy(
        canonical_sportsbook=CANONICAL,
        market_data_provider=PROVIDER,
        checkpoint_windows=WINDOWS,
        max_observation_age_seconds=tolerance,
        retry=retry,
        rules_version=rules_version,
    )


def _cycle(game_id, *, refresh=None, tolerance=CANDIDATE, captured_at=CAPTURE_AT,
           retry=SINGLE_ATTEMPT, policy=None, sleep_fn=lambda _s: None):
    return run_checkpoint_cycle(
        game_id=game_id,
        checkpoint_type="FINAL",
        policy=policy or _policy(tolerance, retry),
        refresh=refresh,
        now_fn=lambda: captured_at,
        sleep_fn=sleep_fn,
    )


def _ok_refresh(market_id, *, books=(CANONICAL, "FANDUEL"), at=None):
    """A refresh that writes fresh quotes and commits, as the real one does."""

    def refresh() -> RefreshOutcome:
        started = (at or CAPTURE_AT) - timedelta(seconds=20)
        for book in books:
            _write_quote(market_id, sportsbook=book, as_of_at=at or CAPTURE_AT)
        return RefreshOutcome(
            ok=True, started_at=started, completed_at=at or CAPTURE_AT, quotes_written=len(books)
        )

    return refresh


def _failed_refresh(error="PROVIDER_TIMEOUT"):
    def refresh() -> RefreshOutcome:
        started = CAPTURE_AT - timedelta(seconds=30)
        return RefreshOutcome(
            ok=False, started_at=started, completed_at=CAPTURE_AT - timedelta(seconds=5), error=error
        )

    return refresh


# --- scenario A: the healthy cycle ------------------------------------


def test_A_refresh_succeeds_immediately_before_capture():
    """The baseline, and the reason healthy captures cannot calibrate the
    tolerance: ages land at ~0 and every candidate looks identical."""

    game_id = _game("A")
    market_id = _market(game_id, "A")

    report = _cycle(game_id, refresh=_ok_refresh(market_id))

    assert report.refresh_ok is True
    assert report.checkpoint_status == "CAPTURED"
    assert report.valid_baselines == 1
    assert report.stale_books_excluded == 0
    assert report.max_observation_age_observed == 0.0
    assert report.refresh_to_capture_seconds == 0.0


def test_the_capture_clock_is_read_after_the_refresh():
    """The 4A.2 regression, now structural. If the cycle read its clock
    before calling refresh, the snapshot could not see the quotes the
    refresh had just written."""

    game_id = _game("clock")
    market_id = _market(game_id, "clock")

    seen: list[str] = []

    def refresh() -> RefreshOutcome:
        seen.append("refresh")
        _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT)
        return RefreshOutcome(ok=True, started_at=CAPTURE_AT, completed_at=CAPTURE_AT, quotes_written=1)

    def now_fn() -> datetime:
        seen.append("clock")
        return CAPTURE_AT

    report = run_checkpoint_cycle(
        game_id=game_id,
        checkpoint_type="FINAL",
        policy=_policy(),
        refresh=refresh,
        now_fn=now_fn,
    )

    # The preflight reads the clock first (it must, to decide eligibility),
    # then the refresh runs, and only THEN is the capture clock read. What
    # matters is that no clock read separates the refresh from the capture.
    assert seen[-2:] == ["refresh", "clock"]
    assert seen.index("refresh") > 0, "the preflight ran before the paid refresh"
    assert report.valid_baselines == 1, "the capture saw the quotes its own refresh wrote"


# --- scenarios B and C: refresh failed --------------------------------


def test_B_refresh_fails_but_prior_observations_are_inside_tolerance():
    """The case the grace period exists for. The refresh is gone; the
    capture still happens, on last-known observations, and says so."""

    game_id = _game("B")
    market_id = _market(game_id, "B")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=600))
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=600))

    report = _cycle(game_id, refresh=_failed_refresh())

    assert report.refresh_ok is False
    assert report.refresh_error == "PROVIDER_TIMEOUT"
    # A failed refresh does NOT abort the capture: a missing checkpoint is
    # a hole in the record, a stale-but-labelled one is information.
    assert report.checkpoint_status == "CAPTURED"
    assert report.valid_baselines == 1
    assert report.canonical_stale == 0
    assert report.max_observation_age_observed == 600.0


def test_C_refresh_fails_and_canonical_exceeds_tolerance():
    """Past the grace period the baseline goes invalid — and nothing gets
    promoted in its place, however fresh."""

    game_id = _game("C")
    market_id = _market(game_id, "C")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=3600))
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=60), line="73.5")

    report = _cycle(game_id, refresh=_failed_refresh())

    assert report.checkpoint_status == "CAPTURED"
    assert report.valid_baselines == 0
    assert report.canonical_stale == 1
    assert report.stale_books_excluded == 1

    with session_scope() as session:
        snap = session.execute(
            __import__("sqlalchemy").select(MarketSnapshot).where(MarketSnapshot.market_id == market_id)
        ).scalars().one()
        assert snap.canonical_sportsbook == CANONICAL, "no substitution"
        assert snap.canonical_line is None
        assert snap.number_of_books == 1, "FanDuel still supplies market context"
        assert snap.books_observed == 2


# --- scenarios D and E: comparison-book coverage ----------------------


def test_D_one_comparison_book_stale_while_canonical_stays_fresh():
    game_id = _game("D")
    market_id = _market(game_id, "D")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=30))
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=30), line="74.5")
    _write_quote(market_id, sportsbook="CAESARS", as_of_at=CAPTURE_AT - timedelta(seconds=7200), line="69.5")

    report = _cycle(game_id, refresh=None)

    assert report.valid_baselines == 1
    assert report.canonical_stale == 0
    assert report.stale_books_excluded == 1
    assert report.books_observed == 3
    assert report.latest_by_book["CAESARS"].included is False
    assert report.latest_by_book[CANONICAL].included is True


def test_E_no_fresh_comparison_books_leaves_a_canonical_only_snapshot():
    """Degraded but usable: the baseline survives, the consensus is a
    one-book median, and the counts say the coverage was degraded rather
    than naturally thin."""

    game_id = _game("E")
    market_id = _market(game_id, "E")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=10))
    for book in ("FANDUEL", "CAESARS", "BETMGM"):
        _write_quote(market_id, sportsbook=book, as_of_at=CAPTURE_AT - timedelta(seconds=9000), line="70.5")

    report = _cycle(game_id, refresh=None)

    assert report.valid_baselines == 1
    assert report.stale_books_excluded == 3
    assert report.books_observed == 4

    with session_scope() as session:
        snap = session.execute(
            __import__("sqlalchemy").select(MarketSnapshot).where(MarketSnapshot.market_id == market_id)
        ).scalars().one()
        assert snap.number_of_books == 1
        assert snap.books_observed == 4
        assert snap.market_min_line == snap.market_max_line == Decimal("74.50")


# --- scenarios F and G: the two clocks are independent ----------------


def test_F_scheduler_late_but_quotes_freshly_refreshed():
    """Large scheduler_offset, small observation ages. A late capture is
    still a real capture: measuring staleness against `target_time` would
    have thrown out quotes fetched seconds earlier."""

    game_id = _game("F")
    market_id = _market(game_id, "F")
    late = TARGET_TIME + timedelta(minutes=50)  # still inside the FINAL window

    report = _cycle(
        game_id,
        refresh=_ok_refresh(market_id, at=late),
        captured_at=late,
    )

    assert report.checkpoint_status == "CAPTURED"
    assert report.scheduler_offset_seconds == pytest.approx(3000.0)
    assert report.max_observation_age_observed == 0.0
    assert report.valid_baselines == 1


def test_G_scheduler_on_time_but_the_earlier_refresh_failed():
    """Small scheduler_offset, large observation ages. The mirror image of
    F, and the reason the two numbers are never merged: they point at
    different problems with different fixes."""

    game_id = _game("G")
    market_id = _market(game_id, "G")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=5400))

    report = _cycle(game_id, refresh=_failed_refresh())

    assert report.scheduler_offset_seconds == 0.0
    assert report.max_observation_age_observed == 5400.0
    assert report.canonical_stale == 1


def test_the_two_clocks_are_never_the_same_number():
    """F and G produce opposite readings from the same pair of fields. If
    anything ever derived one from the other, one of these would break."""

    game_f = _game("Fpair")
    market_f = _market(game_f, "Fpair")
    late = TARGET_TIME + timedelta(minutes=50)
    f = _cycle(game_f, refresh=_ok_refresh(market_f, at=late), captured_at=late)

    game_g = _game("Gpair")
    market_g = _market(game_g, "Gpair")
    _write_quote(market_g, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=5400))
    g = _cycle(game_g, refresh=_failed_refresh())

    assert f.scheduler_offset_seconds > g.scheduler_offset_seconds
    assert f.max_observation_age_observed < g.max_observation_age_observed


# --- the cycle's own guarantees ---------------------------------------


def test_a_bad_tolerance_stops_the_cycle_before_the_refresh_runs():
    """A provider call is money. A misconfigured tolerance must not spend
    one before failing."""

    from app.forecast_lab.quote_selection import FreshnessConfigError

    game_id = _game("badcfg")
    called: list[str] = []

    def refresh() -> RefreshOutcome:
        called.append("spent a credit")
        return RefreshOutcome(ok=True, started_at=CAPTURE_AT, completed_at=CAPTURE_AT)

    with pytest.raises(FreshnessConfigError):
        _cycle(game_id, refresh=refresh, tolerance=-1)
    assert called == []
    # It now fails even earlier: constructing the policy at all is refused,
    # so a misconfiguration cannot reach a scheduler.
    with pytest.raises(FreshnessConfigError):
        _policy(tolerance=-1)


def test_a_second_cycle_is_a_no_op_and_does_not_re_capture():
    """`capture_checkpoint` is idempotent after CAPTURED — which is exactly
    why calibration must never capture a durable game to try a number."""

    game_id = _game("idem")
    market_id = _market(game_id, "idem")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=30))

    first = _cycle(game_id, refresh=None)
    assert first.checkpoint_status == "CAPTURED"

    second = _cycle(game_id, refresh=None, tolerance=1)
    assert second.checkpoint_status == "CAPTURED"

    with session_scope() as session:
        import sqlalchemy

        snapshots = session.execute(
            sqlalchemy.select(MarketSnapshot).where(MarketSnapshot.market_id == market_id)
        ).scalars().all()
        assert len(snapshots) == 1, "the second cycle wrote nothing"
        assert snapshots[0].max_observation_age_seconds == CANDIDATE, "the first run's rule stands"
        runs = session.execute(sqlalchemy.select(CheckpointRun).where(CheckpointRun.game_id == game_id)).scalars().all()
        assert len(runs) == 1


def test_the_cycle_reports_the_latest_observation_per_book():
    game_id = _game("perbook")
    market_id = _market(game_id, "perbook")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=300), tag="dk-old")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=30), tag="dk-new")
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=45), tag="fd")

    report = _cycle(game_id, refresh=None)

    assert report.latest_by_book[CANONICAL].observation_age_seconds == 30.0
    assert report.latest_by_book["FANDUEL"].observation_age_seconds == 45.0
    assert report.latest_by_book[CANONICAL].is_canonical is True
    assert "CANONICAL" in render(report)


# --- calibration preview ----------------------------------------------


def test_calibration_previews_candidates_without_writing_anything():
    game_id = _game("preview")
    market_id = _market(game_id, "preview")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=600))
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=30), line="73.5")

    with session_scope() as session:
        report = preview_thresholds(
            session,
            game_id=game_id,
            taken_at=CAPTURE_AT,
            canonical_sportsbook=CANONICAL,
            market_data_provider=PROVIDER,
            candidates=[None, 3600, 900, 300, 60],
        )
        text = render_calibration(report)

    by_tolerance = {c.max_observation_age_seconds: c for c in report.candidates}
    assert by_tolerance[None].valid_baselines == 1
    assert by_tolerance[3600].valid_baselines == 1
    assert by_tolerance[900].valid_baselines == 1
    assert by_tolerance[300].valid_baselines == 0, "600s-old canonical fails a 300s tolerance"
    assert by_tolerance[300].canonical_stale == 1
    assert by_tolerance[60].canonical_stale == 1
    assert report.tightest_without_baseline_loss().max_observation_age_seconds == 900
    assert "READ ONLY" in text

    # Nothing was written. This is the property that lets calibration run
    # against a durable game without consuming its checkpoint.
    with session_scope() as session:
        import sqlalchemy

        assert session.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(MarketSnapshot)).scalar() == 0
        assert session.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(CheckpointRun)).scalar() == 0


def test_calibration_and_capture_agree_on_the_same_selection():
    """The preview is only evidence about the rule if it IS the rule. If
    these two ever diverge, the preview is measuring its own copy."""

    game_id = _game("agree")
    market_id = _market(game_id, "agree")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=600))
    _write_quote(market_id, sportsbook="FANDUEL", as_of_at=CAPTURE_AT - timedelta(seconds=9000))

    with session_scope() as session:
        preview = preview_thresholds(
            session,
            game_id=game_id,
            taken_at=CAPTURE_AT,
            canonical_sportsbook=CANONICAL,
            market_data_provider=PROVIDER,
            candidates=[CANDIDATE],
        ).candidates[0]

    report = _cycle(game_id, refresh=None)

    assert preview.valid_baselines == report.valid_baselines
    assert preview.stale_books_excluded == report.stale_books_excluded
    assert preview.books_observed == report.books_observed
    assert preview.canonical_stale == report.canonical_stale


def test_calibration_is_read_only():
    """Structural, not a promise in a docstring."""

    import ast
    import inspect

    from app.forecast_lab import calibration

    tree = ast.parse(inspect.getsource(calibration))
    writes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) in {"add", "add_all", "delete", "commit", "flush", "merge"}
    ]
    assert writes == [], "the calibration preview must never write"

    # Matched against CODE, not against the file text. Naming these in a
    # docstring to explain why they are absent must not trip the guard --
    # this exact test has been written wrong twice by matching prose.
    code = ast.unparse(_strip_docstrings(tree))
    for forbidden in ("build_snapshot", "capture_checkpoint", "create_evidence_snapshot", "CheckpointRun"):
        assert forbidden not in code, f"{forbidden} would consume a durable checkpoint"


def _strip_docstrings(tree: "ast.Module") -> "ast.Module":
    import ast as _ast

    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            continue
        body = node.body
        if body and isinstance(body[0], _ast.Expr) and isinstance(body[0].value, _ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [_ast.Pass()]
    return tree
