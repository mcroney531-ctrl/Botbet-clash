"""Phase 4A.5 — cost integrity and the refresh-failure policy.

Two things are under test, and both are about the fact that a capture is
IRREVERSIBLE:

1. A paid refresh must happen only when the checkpoint could actually use
   it. Phase 4A.4 shipped this the wrong way round — it refreshed first
   and discovered the disposition afterwards — so a scheduler re-tick on a
   CAPTURED checkpoint paid for quotes that could never reach it.

2. Because `capture_checkpoint` is idempotent once CAPTURED, "retry by
   calling the cycle again later" cannot work: the first call has already
   consumed the checkpoint. Retries therefore happen BEFORE the capture,
   inside one call, bounded and individually audited (MODEL B).

Every refresh here is a counter, not a network call. Zero credits.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models.ingestion import ProviderCall
from app.db.models.markets import Game, Player, PropMarket
from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.forecast_lab.checkpoint_window import CheckpointDisposition
from app.marketdata.base import ProviderCallMetadata
from app.marketdata.checkpoint_cycle import (
    SINGLE_ATTEMPT,
    CapturePolicy,
    CapturePolicyError,
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
CAPTURE_AT = KICKOFF - timedelta(hours=3)          # inside FINAL, at target
TOO_EARLY_AT = KICKOFF - timedelta(hours=12)       # before window_start
EXPIRED_AT = KICKOFF - timedelta(hours=1)          # after window_end

WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}

CANDIDATE = 900
# The proposed MODEL B budget. Deliberately a test constant, not a default
# anywhere in the code.
PROPOSED_RETRY = RefreshRetryPolicy(max_attempts=3, backoff_seconds=(30.0, 120.0))


# --- fixtures ---------------------------------------------------------


class CountingRefresh:
    """Records every invocation. The whole cost-integrity suite is
    assertions about this counter."""

    def __init__(self, *, outcomes: list[RefreshOutcome] | None = None, on_call=None):
        self.calls = 0
        self.outcomes = outcomes
        self.on_call = on_call

    def __call__(self) -> RefreshOutcome:
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.outcomes:
            index = min(self.calls - 1, len(self.outcomes) - 1)
            return self.outcomes[index]
        return RefreshOutcome(ok=True, started_at=CAPTURE_AT, completed_at=CAPTURE_AT, quotes_written=1)


def _ok(quotes=1) -> RefreshOutcome:
    return RefreshOutcome(
        ok=True, started_at=CAPTURE_AT, completed_at=CAPTURE_AT, quotes_written=quotes
    )


def _fail(category="TIMEOUT") -> RefreshOutcome:
    return RefreshOutcome(
        ok=False,
        started_at=CAPTURE_AT,
        completed_at=CAPTURE_AT,
        error=f"simulated {category}",
        error_category=category,
    )


def _game(tag: str) -> uuid.UUID:
    with session_scope() as session:
        season = Season(year=2026, name=f"pre-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        game = Game(
            external_ref=f"pre-{tag}",
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
        player = Player(external_ref=f"GSIS:pre-{tag}", name=f"Player {tag}")
        session.add(player)
        session.flush()
        market = PropMarket(game_id=game_id, player_id=player.id, stat_type="receiving_yards")
        session.add(market)
        session.flush()
        return market.id


def _write_quote(market_id: uuid.UUID, *, sportsbook: str, as_of_at: datetime, tag=None):
    with session_scope() as session:
        run = start_run(session, provider=PROVIDER, operation="FETCH_QUOTES")
        call = record_call(
            session,
            run=run,
            metadata=ProviderCallMetadata(
                endpoint_capability="FETCH_EVENT_ODDS",
                requested_at=as_of_at,
                responded_at=as_of_at,
                http_status=200,
                raw_response_body=f"json-{tag or as_of_at.isoformat()}".encode(),
                raw_response_sha256="e" * 64,
                raw_response_bytes=12,
            ),
            success=True,
        )
        row = IngestionService(session).persist_quote(
            quote=ProviderQuote(
                event=ProviderEventRef(provider=PROVIDER, external_event_id=f"e-{market_id}"),
                player=ProviderPlayerRef(provider=PROVIDER, display_name="Player X"),
                stat_family=StatFamily.RECEIVING_YARDS,
                vendor_market_key="player_reception_yds",
                sportsbook=sportsbook,
                line=Decimal("74.5"),
                over_price=-165,
                under_price=129,
                as_of_at=as_of_at,
                retrieved_at=as_of_at,
                source=PROVIDER,
            ),
            market_id=market_id,
            provider_call=call,
        )
        assert row is not None


def _policy(tolerance=CANDIDATE, retry=SINGLE_ATTEMPT, rules_version=None) -> CapturePolicy:
    return CapturePolicy(
        canonical_sportsbook=CANONICAL,
        market_data_provider=PROVIDER,
        checkpoint_windows=WINDOWS,
        max_observation_age_seconds=tolerance,
        retry=retry,
        rules_version=rules_version,
    )


def _cycle(game_id, *, refresh, at=CAPTURE_AT, policy=None):
    return run_checkpoint_cycle(
        game_id=game_id,
        checkpoint_type="FINAL",
        policy=policy or _policy(),
        refresh=refresh,
        now_fn=lambda: at,
        sleep_fn=lambda _s: None,
    )


# =====================================================================
# 1. COST INTEGRITY — zero provider calls when the checkpoint cannot use them
# =====================================================================


def test_an_already_captured_checkpoint_costs_zero_provider_calls():
    """The 4A.4 bug. A scheduler that ticks twice — or a retry loop —
    would have paid for quotes that could never reach an immutable
    checkpoint, once per tick, forever."""

    game_id = _game("captured")
    market_id = _market(game_id, "captured")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=30))

    first = CountingRefresh()
    r1 = _cycle(game_id, refresh=first)
    assert r1.checkpoint_status == "CAPTURED"
    assert first.calls == 1

    second = CountingRefresh()
    r2 = _cycle(game_id, refresh=second)
    assert second.calls == 0, "a captured checkpoint must never pay again"
    assert r2.provider_calls_spent == 0
    assert r2.disposition is CheckpointDisposition.ALREADY_CAPTURED
    assert r2.refresh_skipped_reason == "ALREADY_CAPTURED"
    assert r2.checkpoint_status == "CAPTURED"


def test_a_call_before_the_window_costs_zero_provider_calls():
    game_id = _game("early")
    _market(game_id, "early")

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh, at=TOO_EARLY_AT)

    assert refresh.calls == 0
    assert report.provider_calls_spent == 0
    assert report.disposition is CheckpointDisposition.TOO_EARLY
    assert report.checkpoint_status == "PENDING"


def test_a_call_after_the_window_costs_zero_provider_calls():
    """The MISSED path still runs — it is database bookkeeping and free.
    Only the provider call is skipped."""

    game_id = _game("expired")
    _market(game_id, "expired")

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh, at=EXPIRED_AT)

    assert refresh.calls == 0
    assert report.provider_calls_spent == 0
    assert report.disposition is CheckpointDisposition.EXPIRED
    assert report.checkpoint_status == "MISSED"


def test_an_eligible_call_does_refresh():
    """The counterpart. A preflight that refused everything would pass all
    three tests above and be useless."""

    game_id = _game("eligible")
    _market(game_id, "eligible")

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh)

    assert refresh.calls == 1
    assert report.disposition is CheckpointDisposition.ELIGIBLE
    assert report.provider_calls_spent == 1


def test_an_invalid_policy_costs_zero_provider_calls():
    """A misconfigured tolerance is refused at policy construction, which
    is before a scheduler can even build a cycle."""

    from app.forecast_lab.quote_selection import FreshnessConfigError

    with pytest.raises(FreshnessConfigError):
        _policy(tolerance=-1)
    with pytest.raises(CapturePolicyError):
        RefreshRetryPolicy(max_attempts=0)
    with pytest.raises(CapturePolicyError):
        RefreshRetryPolicy(max_attempts=2, backoff_seconds=(30.0, 60.0))


def test_the_preflight_writes_nothing():
    """It answers a question; it must not create the state it was asked
    about. A too-early poll that silently created a PENDING row would mean
    reading changed the record."""

    from app.db.models.markets import CheckpointRun
    import sqlalchemy

    game_id = _game("nowrite")
    _market(game_id, "nowrite")

    with session_scope() as session:
        from app.marketdata.checkpoint_cycle import preflight_checkpoint

        game = session.get(Game, game_id)
        pre = preflight_checkpoint(
            session, game=game, checkpoint_type="FINAL",
            windows_config=WINDOWS, now=TOO_EARLY_AT,
        )
        assert pre.disposition is CheckpointDisposition.TOO_EARLY
        assert pre.existing_status is None

    with session_scope() as session:
        count = session.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(CheckpointRun)
        ).scalar()
        assert count == 0, "the preflight created a row"


def test_eligibility_has_one_implementation():
    """If the preflight and the capture could disagree, the disagreement
    would be paid for in credits on every tick."""

    import ast
    import inspect

    from app.forecast_lab import checkpoint_service
    from app.marketdata import checkpoint_cycle

    for module in (checkpoint_service, checkpoint_cycle):
        code = ast.unparse(ast.parse(inspect.getsource(module)))
        assert "window_start" not in code or "disposition" in code
        # Neither module may re-derive the boundaries itself.
        assert "start_hours_before_kickoff" not in code, (
            f"{module.__name__} recomputes window arithmetic instead of using "
            "checkpoint_window.compute_window"
        )


# =====================================================================
# 2. MODEL B — bounded retries BEFORE the irreversible capture
# =====================================================================


def test_a_retryable_failure_is_retried_and_a_later_success_is_used():
    """One transient timeout must not permanently consume FINAL."""

    game_id = _game("retry-ok")
    market_id = _market(game_id, "retry-ok")

    def write_on_second_call():
        if refresh.calls == 2:
            _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT)

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT"), _ok()], on_call=write_on_second_call)
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert refresh.calls == 2
    assert report.refresh_ok is True
    assert [a.outcome.error_category for a in report.attempts] == ["TIMEOUT", None]
    assert report.attempts[1].waited_seconds == 30.0
    assert report.valid_baselines == 1
    assert report.max_observation_age_observed == 0.0


def test_the_retry_budget_is_bounded_and_then_the_capture_happens_anyway():
    """Fail loud, not fail silent. When the budget is exhausted the
    checkpoint still captures on last-known observations and the freshness
    gate labels them — a missing checkpoint is a hole in the record."""

    game_id = _game("retry-exhaust")
    market_id = _market(game_id, "retry-exhaust")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(hours=16))

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT")])
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert refresh.calls == 3, "exactly max_attempts, no more"
    assert report.refresh_ok is False
    assert "budget" in report.refresh_skipped_reason
    assert report.checkpoint_status == "CAPTURED"
    assert report.canonical_stale == 1
    assert report.valid_baselines == 0
    assert [a.waited_seconds for a in report.attempts] == [0.0, 30.0, 120.0]


@pytest.mark.parametrize(
    "category", ["AUTHENTICATION_ERROR", "QUOTA_EXHAUSTED", "QUOTA_RESERVE_EXHAUSTED", "MALFORMED_RESPONSE"]
)
def test_a_non_retryable_failure_is_not_retried(category):
    """Retrying these cannot succeed. MALFORMED_RESPONSE is the sharpest
    case: the provider ANSWERED and we were billed, so the same request
    yields the same unusable payload and a retry pays again for it."""

    game_id = _game(f"nonretry-{category.lower()}")
    _market(game_id, f"nonretry-{category.lower()}")

    refresh = CountingRefresh(outcomes=[_fail(category)])
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert refresh.calls == 1, f"{category} must not be retried"
    assert "not retryable" in report.refresh_skipped_reason
    assert report.checkpoint_status == "CAPTURED"


def test_retries_never_consume_the_window_they_protect():
    """A retry that would leave too little of the window is not attempted.
    Spending the window on retries would turn a recoverable failure into a
    MISSED checkpoint."""

    game_id = _game("window-guard")
    _market(game_id, "window-guard")

    # 100 seconds before window_end: the 120s second backoff cannot fit.
    late = KICKOFF - timedelta(hours=2) - timedelta(seconds=100)

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT")])
    report = _cycle(
        game_id, refresh=refresh, at=late,
        policy=_policy(retry=RefreshRetryPolicy(max_attempts=3, backoff_seconds=(120.0,))),
    )

    assert refresh.calls == 1
    assert "guard" in report.refresh_skipped_reason
    assert report.checkpoint_status == "CAPTURED"


def test_model_a_is_the_single_attempt_policy():
    """The default stays one shot. MODEL B is opt-in through frozen rules,
    not something a cycle acquires by accident."""

    game_id = _game("model-a")
    _market(game_id, "model-a")

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT")])
    report = _cycle(game_id, refresh=refresh)

    assert SINGLE_ATTEMPT.max_attempts == 1
    assert refresh.calls == 1
    assert report.checkpoint_status == "CAPTURED"


def test_each_attempt_is_separately_auditable():
    """Three billed calls must not look like one. An adapter-level retry
    would hide exactly this."""

    game_id = _game("audit")
    _market(game_id, "audit")

    refresh = CountingRefresh(
        outcomes=[_fail("TIMEOUT"), _fail("PROVIDER_UNAVAILABLE"), _fail("RATE_LIMITED")]
    )
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert [a.attempt for a in report.attempts] == [1, 2, 3]
    assert [a.outcome.error_category for a in report.attempts] == [
        "TIMEOUT", "PROVIDER_UNAVAILABLE", "RATE_LIMITED",
    ]
    assert report.provider_calls_spent == 3
    assert report.refresh_error == "simulated RATE_LIMITED", "the LAST attempt's error"
    text = render(report)
    assert "attempt 3" in text and "RATE_LIMITED" in text


def test_the_capture_clock_is_read_after_the_final_attempt():
    """Not after the first. Reading it earlier would make snapshots claim a
    taken_at before the quotes a later successful retry wrote."""

    game_id = _game("clock-after-retries")
    market_id = _market(game_id, "clock-after-retries")
    order: list[str] = []

    def note():
        order.append(f"attempt{refresh.calls}")
        if refresh.calls == 2:
            _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT)

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT"), _ok()], on_call=note)

    def now_fn():
        order.append("clock")
        return CAPTURE_AT

    report = run_checkpoint_cycle(
        game_id=game_id,
        checkpoint_type="FINAL",
        policy=_policy(retry=PROPOSED_RETRY),
        refresh=refresh,
        now_fn=now_fn,
        sleep_fn=lambda _s: None,
    )

    assert order[-1] == "clock"
    assert order.index("attempt2") < len(order) - 1
    assert report.valid_baselines == 1


def test_a_checkpoint_still_captures_at_most_once_with_retries():
    import sqlalchemy

    from app.db.models.markets import CheckpointRun, MarketSnapshot

    game_id = _game("once")
    market_id = _market(game_id, "once")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=10))

    _cycle(game_id, refresh=CountingRefresh(outcomes=[_fail("TIMEOUT")]),
           policy=_policy(retry=PROPOSED_RETRY))
    _cycle(game_id, refresh=CountingRefresh(), policy=_policy(retry=PROPOSED_RETRY))

    with session_scope() as session:
        runs = session.execute(sqlalchemy.select(CheckpointRun)).scalars().all()
        snaps = session.execute(
            sqlalchemy.select(MarketSnapshot).where(MarketSnapshot.market_id == market_id)
        ).scalars().all()
    assert len(runs) == 1 and len(snaps) == 1


# =====================================================================
# 3. THE POLICY COMES FROM FROZEN RULES
# =====================================================================


def _rules(session, season_id, **overrides):
    fields = dict(
        season_id=season_id,
        rules_version=f"test-{uuid.uuid4()}",
        starting_bankroll_cents=1500,
        canonical_sportsbook=CANONICAL,
        market_data_provider=PROVIDER,
        roster_data_provider="NFLVERSE",
        research_settlement_provider="NFLVERSE",
        research_settlement_delay_hours=24,
        supported_prop_types=["receiving_yards"],
        devig_method="PROPORTIONAL_V1",
        benchmark_slate_size=5,
        batch_methodology="SINGLE_BATCH",
        checkpoint_windows=WINDOWS,
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake_cents=25,
        stake_increment_cents=25,
        pounce_limit=1,
        attribution_confidence_threshold=Decimal("0.700"),
        effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    row = SeasonRules(**fields)
    session.add(row)
    session.flush()
    return row


def test_an_official_policy_comes_from_frozen_rules_and_says_so():
    """`rules_version` is the provenance. "An operator picked a threshold
    by hand for this run" must not look the same as "the season's rules
    said so"."""

    game_id = _game("official")
    with session_scope() as session:
        game = session.get(Game, game_id)
        rules = _rules(
            session, game.season_id,
            max_observation_age_seconds=900,
            refresh_retry_policy={"max_attempts": 3, "backoff_seconds": [30, 120]},
        )
        version = rules.rules_version
        policy = CapturePolicy.from_season_rules(session, season_id=game.season_id)

    assert policy.is_official is True
    assert policy.rules_version == version
    assert policy.max_observation_age_seconds == 900
    assert policy.retry.max_attempts == 3
    assert policy.retry.backoff_seconds == (30.0, 120.0)
    assert policy.canonical_sportsbook == CANONICAL
    assert policy.market_data_provider == PROVIDER


def test_an_injected_policy_is_not_official():
    assert _policy().is_official is False
    assert _policy(rules_version="2026-w0.1").is_official is True


def test_a_season_with_no_frozen_policy_gets_no_gate_and_one_attempt():
    """NULL means no capture policy was frozen, which is exactly what every
    pre-4A.5 season ran under. It is not a zero-second tolerance and not
    zero attempts."""

    game_id = _game("legacy")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(session, game.season_id)
        policy = CapturePolicy.from_season_rules(session, season_id=game.season_id)

    assert policy.max_observation_age_seconds is None
    assert policy.retry == SINGLE_ATTEMPT


def test_a_superseded_rules_row_is_not_used():
    """A rules amendment is a new row. Resolving to the superseded one
    would run a capture under rules that were explicitly replaced."""

    game_id = _game("amended")
    with session_scope() as session:
        game = session.get(Game, game_id)
        old = _rules(
            session, game.season_id, max_observation_age_seconds=60,
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        new = _rules(
            session, game.season_id, max_observation_age_seconds=900,
            effective_from=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )
        old.superseded_by = new.id
        session.flush()
        policy = CapturePolicy.from_season_rules(session, season_id=game.season_id)

    assert policy.max_observation_age_seconds == 900


def test_the_database_refuses_a_negative_frozen_tolerance():
    from sqlalchemy.exc import IntegrityError

    game_id = _game("badrules")
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            game = session.get(Game, game_id)
            _rules(session, game.season_id, max_observation_age_seconds=-1)


def test_no_existing_season_was_given_a_tolerance_by_the_migration():
    """The migration is additive and sets no value. Assigning 900 to a
    season is a deliberate provisioning decision, not a schema change."""

    import sqlalchemy

    with session_scope() as session:
        rows = session.execute(
            sqlalchemy.select(SeasonRules.max_observation_age_seconds)
        ).scalars().all()
    assert all(v is None for v in rows)
