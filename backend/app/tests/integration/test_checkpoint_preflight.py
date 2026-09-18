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
    CapturePolicyNotFrozen,
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


def test_the_first_paid_attempt_is_guarded_by_the_remaining_window():
    """ELIGIBLE at this instant is NOT "there is enough window left to
    sensibly begin network work".

    A cycle starting seconds before window_end would buy a refresh and
    then cross the boundary, so the capture it paid for is marked MISSED.
    The guard is sized from the real call sequence: an nflverse roster
    download (60s timeout) plus fetch_quotes (30s), then a reserve for the
    capture itself. The budget carries headroom over that sum rather than
    equalling it.
    """

    game_id = _game("first-guard")
    _market(game_id, "first-guard")

    # Inside the window, but with less room than one refresh needs.
    late = KICKOFF - timedelta(hours=2) - timedelta(seconds=100)

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh, at=late, policy=_policy(retry=PROPOSED_RETRY))

    assert report.disposition is CheckpointDisposition.ELIGIBLE, "still eligible..."
    assert refresh.calls == 0, "...but not worth starting"
    assert report.provider_calls_spent == 0
    assert "window left" in report.refresh_skipped_reason


def test_the_window_guard_boundary():
    """Just enough room refreshes; one second less does not."""

    room = PROPOSED_RETRY.room_needed_seconds
    window_end = KICKOFF - timedelta(hours=2)

    enough = _game("guard-enough")
    _market(enough, "guard-enough")
    refresh_a = CountingRefresh()
    _cycle(enough, refresh=refresh_a, at=window_end - timedelta(seconds=room),
           policy=_policy(retry=PROPOSED_RETRY))
    assert refresh_a.calls == 1

    short = _game("guard-short")
    _market(short, "guard-short")
    refresh_b = CountingRefresh()
    _cycle(short, refresh=refresh_b, at=window_end - timedelta(seconds=room - 1),
           policy=_policy(retry=PROPOSED_RETRY))
    assert refresh_b.calls == 0


def test_retries_never_consume_the_window_they_protect():
    """The same guard, offset by the backoff. Spending the window on
    retries would turn a recoverable failure into a MISSED checkpoint.

    A small request budget is used here so attempt 1 clears the guard and
    the retry is the thing being refused -- otherwise this would just be
    the attempt-1 test again.
    """

    game_id = _game("window-guard")
    _market(game_id, "window-guard")

    tight = RefreshRetryPolicy(
        max_attempts=3, backoff_seconds=(120.0,),
        window_guard_seconds=10.0, request_budget_seconds=10.0,
    )
    # 100s left: attempt 1 needs 20s of room and fits; the 120s backoff
    # would leave -20s and must not be attempted.
    late = KICKOFF - timedelta(hours=2) - timedelta(seconds=100)

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT")])
    report = _cycle(game_id, refresh=refresh, at=late, policy=_policy(retry=tight))

    assert refresh.calls == 1
    assert "guard" in report.refresh_skipped_reason
    assert report.checkpoint_status == "CAPTURED"


def test_the_two_guards_are_one_rule():
    """Two separate guards would inevitably disagree about how much room a
    paid attempt needs."""

    import ast
    import inspect

    from app.marketdata import checkpoint_cycle

    code = ast.unparse(ast.parse(inspect.getsource(checkpoint_cycle)))
    assert code.count("_has_room(") >= 2, "both call sites must use the shared helper"
    assert "room_needed_seconds" in code


def test_the_request_budget_covers_the_real_call_sequence():
    """Sized against the ACTUAL sequence, not a remembered one.

    The production refresh is two calls -- an nflverse roster download and
    one Odds fetch_quotes -- because the event identity is already
    persisted. The budget must cover both timeouts with room to spare;
    tying it to the exact sum would make it fail the moment either adapter
    changed its timeout by a second.
    """

    from app.marketdata.checkpoint_cycle import DEFAULT_REQUEST_BUDGET_SECONDS
    from app.marketdata.providers.the_odds_api import DEFAULT_TIMEOUT_SECONDS as ODDS_TIMEOUT
    from app.rosterdata.providers.nflverse import DEFAULT_TIMEOUT_SECONDS as ROSTER_TIMEOUT

    timeout_sum = ROSTER_TIMEOUT + ODDS_TIMEOUT
    assert DEFAULT_REQUEST_BUDGET_SECONDS >= timeout_sum, (
        f"a {DEFAULT_REQUEST_BUDGET_SECONDS}s budget cannot cover {timeout_sum}s "
        "of provider timeouts"
    )
    # Headroom, deliberately: connection setup, redirects and parsing all
    # sit outside the per-request timeouts.
    assert DEFAULT_REQUEST_BUDGET_SECONDS > timeout_sum


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


def test_is_official_means_more_than_a_rules_version_existing():
    """The masquerade this closeout fixes. A real season row with NULL
    policy fields carries a rules_version, so keying `is_official` on that
    alone would report "no freshness gate, one attempt" as an official
    policy -- which is precisely the state meaning nothing has been frozen.
    """

    assert _policy().is_official is False
    # A version string alone is not enough any more.
    assert _policy(rules_version="2026-w0.1").is_official is False
    # Nor is a version plus a tolerance, while the retry policy is unfrozen.
    assert CapturePolicy(
        canonical_sportsbook=CANONICAL,
        market_data_provider=PROVIDER,
        checkpoint_windows=WINDOWS,
        max_observation_age_seconds=900,
        rules_version="2026-w0.1",
    ).is_official is False
    # All three, and only then.
    assert CapturePolicy(
        canonical_sportsbook=CANONICAL,
        market_data_provider=PROVIDER,
        checkpoint_windows=WINDOWS,
        max_observation_age_seconds=900,
        retry=PROPOSED_RETRY,
        rules_version="2026-w0.1",
        retry_frozen=True,
    ).is_official is True


def test_a_real_provider_season_without_a_frozen_policy_fails_closed():
    """The blocker this closeout exists for.

    The durable BotBet 2026 season currently has both new fields NULL. The
    old code turned that into "no freshness gate, one attempt" AND reported
    it as official, because rules_version was populated. NULL means no
    decision has been made; it is not a decision to disable the gate.
    """

    game_id = _game("notfrozen")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(session, game.season_id)  # both policy fields NULL
        with pytest.raises(CapturePolicyNotFrozen, match="max_observation_age_seconds"):
            CapturePolicy.from_season_rules(session, season_id=game.season_id)


def test_a_half_frozen_policy_also_fails_closed():
    """A tolerance without a retry budget is still an undecided policy, and
    the error names the field that is missing."""

    game_id = _game("halffrozen")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(session, game.season_id, max_observation_age_seconds=900)
        with pytest.raises(CapturePolicyNotFrozen, match="refresh_retry_policy"):
            CapturePolicy.from_season_rules(session, season_id=game.season_id)


def test_a_synthetic_season_still_resolves_but_is_not_official():
    """Synthetic seasons fabricate their own market data and have no
    provider to overspend against, so they keep working. What they must not
    do is look frozen."""

    from app.marketdata.provenance import SYNTHETIC_SOURCE

    game_id = _game("synthetic")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(session, game.season_id, market_data_provider=SYNTHETIC_SOURCE)
        policy = CapturePolicy.from_season_rules(session, season_id=game.season_id)

    assert policy.max_observation_age_seconds is None
    assert policy.retry == SINGLE_ATTEMPT
    assert policy.is_official is False


def test_a_superseded_rules_row_is_not_used():
    """A rules amendment is a new row. Resolving to the superseded one
    would run a capture under rules that were explicitly replaced."""

    game_id = _game("amended")
    with session_scope() as session:
        game = session.get(Game, game_id)
        old = _rules(
            session, game.season_id, max_observation_age_seconds=60,
            refresh_retry_policy={"max_attempts": 1},
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        new = _rules(
            session, game.season_id, max_observation_age_seconds=900,
            refresh_retry_policy={"max_attempts": 3, "backoff_seconds": [30, 120]},
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


# =====================================================================
# 4. CONCURRENCY — the lease closes the double-spend race
# =====================================================================


def test_two_racing_workers_produce_exactly_one_paid_refresh():
    """The hole the read-only preflight could not close.

        worker A: preflight -> ELIGIBLE
        worker B: preflight -> ELIGIBLE
        worker A: pays
        worker B: pays

    Both paid, for a checkpoint only one could capture. Run here with real
    threads against real Postgres, because the claim's atomicity is a
    property of one SQL statement, not of application logic — a test that
    simulated the race in one process would prove nothing about it.
    """

    import threading

    game_id = _game("race")
    market_id = _market(game_id, "race")

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    paid: list[str] = []

    def worker(name: str, results: dict):
        def refresh() -> RefreshOutcome:
            with lock:
                paid.append(name)
            _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT, tag=name)
            return _ok()

        barrier.wait()  # both workers enter the cycle at the same instant
        results[name] = run_checkpoint_cycle(
            game_id=game_id,
            checkpoint_type="FINAL",
            policy=_policy(retry=PROPOSED_RETRY),
            refresh=refresh,
            now_fn=lambda: CAPTURE_AT,
            sleep_fn=lambda _s: None,
            owner=name,
        )

    results: dict = {}
    threads = [threading.Thread(target=worker, args=(n, results)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(paid) == 1, f"both workers paid: {paid}"

    winner = paid[0]
    loser = "B" if winner == "A" else "A"
    assert results[winner].provider_calls_spent == 1
    assert results[loser].provider_calls_spent == 0

    # Two legitimate ways to lose, depending on how the threads interleave:
    # blocked by the live lease, or arriving after the winner had already
    # captured. Both are correct; asserting only one would make this test
    # flaky rather than strict. What must hold either way is that the loser
    # spent nothing.
    reason = results[loser].refresh_skipped_reason
    assert "lease" in reason or "CAPTURED" in reason, reason


def test_the_loser_does_not_capture_either():
    """A worker that lost the lease has no fresh data and the holder is
    mid-refresh. Capturing would freeze a snapshot built on pre-refresh
    state AND consume the checkpoint out from under the holder — and
    capture is irreversible."""

    game_id = _game("loser")
    market_id = _market(game_id, "loser")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT - timedelta(seconds=30))

    from app.marketdata.checkpoint_lease import claim_cycle

    held = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL", now=CAPTURE_AT,
        duration_seconds=600, owner="holder",
    )
    assert held.acquired is True

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert refresh.calls == 0
    assert report.lease_acquired is False
    assert report.checkpoint_status == "NOT_RUN", "no capture while another worker owns the cycle"


def test_an_expired_lease_is_reclaimed():
    """A worker that dies mid-cycle must not block the checkpoint forever.
    The lease lapses and the next worker takes it."""

    from app.marketdata.checkpoint_lease import claim_cycle

    game_id = _game("expired-lease")
    _market(game_id, "expired-lease")

    dead = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL",
        now=CAPTURE_AT - timedelta(hours=1), duration_seconds=60, owner="crashed",
    )
    assert dead.acquired is True

    refresh = CountingRefresh()
    report = _cycle(game_id, refresh=refresh, policy=_policy(retry=PROPOSED_RETRY))

    assert refresh.calls == 1, "the abandoned lease was reclaimed"
    assert report.lease_acquired is True


def test_an_unexpired_lease_cannot_be_stolen():
    """The counterpart. If expiry alone reclaimed a lease, a live holder
    would be trampled."""

    from app.marketdata.checkpoint_lease import claim_cycle

    game_id = _game("live-lease")
    _market(game_id, "live-lease")

    first = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL", now=CAPTURE_AT,
        duration_seconds=600, owner="holder",
    )
    second = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL", now=CAPTURE_AT + timedelta(seconds=5),
        duration_seconds=600, owner="intruder",
    )
    assert first.acquired is True
    assert second.acquired is False
    assert second.held_by == "holder"


def test_the_lease_is_released_so_a_retry_of_the_whole_cycle_is_possible():
    """The claim is held for the cycle, not for the checkpoint's life. A
    cycle that refreshed and captured must not leave a lease behind."""

    import sqlalchemy

    from app.db.models.markets import CheckpointCycleLease

    game_id = _game("release")
    market_id = _market(game_id, "release")
    _write_quote(market_id, sportsbook=CANONICAL, as_of_at=CAPTURE_AT)

    report = _cycle(game_id, refresh=CountingRefresh(), policy=_policy(retry=PROPOSED_RETRY))
    assert report.checkpoint_status == "CAPTURED"

    with session_scope() as session:
        remaining = session.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(CheckpointCycleLease)
        ).scalar()
    assert remaining == 0


def test_a_released_lease_is_scoped_to_its_own_id():
    """A worker that overran its lease, and whose claim has since been
    taken over, must not delete the new holder's lease on the way out."""

    from app.marketdata.checkpoint_lease import claim_cycle, release_cycle

    game_id = _game("scoped-release")
    _market(game_id, "scoped-release")

    overran = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL",
        now=CAPTURE_AT - timedelta(hours=1), duration_seconds=60, owner="overran",
    )
    took_over = claim_cycle(
        game_id=game_id, checkpoint_type="FINAL", now=CAPTURE_AT,
        duration_seconds=600, owner="took-over",
    )
    assert took_over.acquired is True

    removed = release_cycle(
        game_id=game_id, checkpoint_type="FINAL", lease_id=overran.lease_id
    )
    assert removed is False, "the overrunning worker deleted someone else's lease"


def test_no_database_transaction_spans_the_network_call():
    """The claim and the release are each their own short transaction. A
    lock held across an unbounded provider wait is the one thing this whole
    choreography exists to prevent."""

    import ast
    import inspect

    from app.marketdata import checkpoint_lease

    # Matched against CODE, not the file text: the module docstring
    # legitimately explains why SELECT FOR UPDATE is unusable here, and
    # matching prose has burned this pattern three times already.
    tree = ast.parse(inspect.getsource(checkpoint_lease))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert "FOR UPDATE" not in code.upper()
    assert code.count("session_scope()") == 2, "one short transaction each for claim and release"


# =====================================================================
# 5. STRICT VALIDATION OF FROZEN POLICY JSON
# =====================================================================


@pytest.mark.parametrize(
    "record, why",
    [
        ({"max_attempts": "3"}, "a string would int() cleanly to 3"),
        ({"max_attempts": True}, "bool is an int in Python"),
        ({"max_attempts": 3.7}, "int() truncates rather than rejecting"),
        ({"max_attempts": 3, "backoff_seconds": [30, -1]}, "negative delay"),
        ({"max_attempts": 3, "backoff_seconds": [30, "120"]}, "string delay"),
        ({"max_attempts": 3, "backoff_seconds": [30, float("inf")]}, "non-finite delay"),
        ({"max_attempts": 3, "backoff_seconds": "30,120"}, "not an array"),
        ({"max_attempts": 3, "max_attempt": 4}, "misspelled key would take its default"),
        ({"backoff_seconds": [30]}, "no max_attempts at all"),
        ({"max_attempts": 3, "window_guard_seconds": True}, "bool guard"),
        ("not-an-object", "not an object"),
    ],
)
def test_malformed_frozen_policy_is_rejected_not_coerced(record, why):
    """These rules are the research contract. `int("3")`, `int(True)` and
    `int(3.7)` all succeed in Python and would quietly turn a typo into a
    plausible-looking policy that changes how many times we are willing to
    pay."""

    with pytest.raises(CapturePolicyError):
        RefreshRetryPolicy.from_record(record)


def test_the_proposed_v1_record_round_trips():
    record = {
        "max_attempts": 3,
        "backoff_seconds": [30, 120],
        "window_guard_seconds": 60,
        "request_budget_seconds": 180,
    }
    policy = RefreshRetryPolicy.from_record(record)
    assert policy.max_attempts == 3
    assert policy.backoff_seconds == (30.0, 120.0)
    assert policy.as_record() == {
        "max_attempts": 3,
        "backoff_seconds": [30.0, 120.0],
        "window_guard_seconds": 60.0,
        "request_budget_seconds": 180.0,
    }


def test_none_stays_none_so_the_caller_decides():
    """`from_record` must not invent SINGLE_ATTEMPT: that is how "no policy
    frozen" became "one attempt, officially"."""

    assert RefreshRetryPolicy.from_record(None) is None


def test_a_malformed_policy_fails_before_any_provider_call():
    game_id = _game("malformed")
    _market(game_id, "malformed")

    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(
            session, game.season_id,
            max_observation_age_seconds=900,
            refresh_retry_policy={"max_attempts": "3"},
        )
        with pytest.raises(CapturePolicyError):
            CapturePolicy.from_season_rules(session, season_id=game.season_id)


# =====================================================================
# 6. OFFICIAL RUNNER + APPEND-ONLY RULES AMENDMENT
# =====================================================================

V1_RETRY = {
    "max_attempts": 3,
    "backoff_seconds": [30, 120],
    "window_guard_seconds": 60,
    "request_budget_seconds": 180,
}


def test_the_official_runner_takes_no_policy_arguments():
    """Structural. An operator must not be able to choose the tolerance,
    the retry budget, the canonical book or the provider for a real
    capture -- all four change which market state reaches an irreversible
    capture."""

    import inspect

    from app.marketdata import official_capture

    signature = inspect.signature(official_capture.run_official_checkpoint)
    # `refresh` and `sleep_fn` are test seams, not operator options -- the
    # CLI check below proves neither is reachable from the command line.
    # There is deliberately NO now_fn: an official capture must not be told
    # what time it is.
    assert set(signature.parameters) == {
        "game_id", "checkpoint_type", "refresh", "owner", "sleep_fn"
    }
    assert "now_fn" not in signature.parameters

    source = inspect.getsource(official_capture)
    for flag in (
        "--max-observation-age-seconds",
        "--max-attempts",
        "--canonical-sportsbook",
        "--market-data-provider",
    ):
        assert flag not in source, f"{flag} must not be an official-capture option"


def test_the_official_runner_refuses_an_unfrozen_policy():
    game_id = _game("official-unfrozen")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(session, game.season_id)

    from app.marketdata.official_capture import run_official_checkpoint

    refresh = CountingRefresh()
    with pytest.raises(CapturePolicyNotFrozen):
        run_official_checkpoint(game_id=game_id, checkpoint_type="FINAL", refresh=refresh, sleep_fn=lambda _s: None)
    assert refresh.calls == 0


def test_the_official_runner_resolves_the_frozen_policy():
    game_id = _game("official-frozen")
    with session_scope() as session:
        game = session.get(Game, game_id)
        _rules(
            session, game.season_id,
            max_observation_age_seconds=900,
            refresh_retry_policy=V1_RETRY,
        )

    from app.marketdata.official_capture import resolve_policy

    policy = resolve_policy(game_id=game_id)
    assert policy.is_official is True
    assert policy.max_observation_age_seconds == 900
    assert policy.retry.max_attempts == 3


def test_the_amendment_is_append_only_and_changes_only_the_policy():
    """SeasonRules is versioned. An UPDATE would rewrite the rules a
    checkpoint was already captured under, and every stored snapshot is
    only interpretable against the rules in force when it was written."""

    import sqlalchemy

    from app.services.amend_capture_policy import apply_amendment

    game_id = _game("amend")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        original = _rules(session, season_id, rules_version="2026-w0.1")
        original_id = original.id
        before = {f: getattr(original, f) for f in ("kelly_fraction", "pounce_limit", "checkpoint_windows")}

    with session_scope() as session:
        clone = apply_amendment(
            session,
            season_id=season_id,
            max_observation_age_seconds=900,
            refresh_retry_policy=V1_RETRY,
            rules_version="2026-w0.2",
            amendment_reason="Week 0 capture freshness and refresh-retry policy freeze",
            effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        clone_id = clone.id

    with session_scope() as session:
        rows = session.execute(
            sqlalchemy.select(SeasonRules).where(SeasonRules.season_id == season_id)
        ).scalars().all()
        by_id = {r.id: r for r in rows}

        assert len(rows) == 2, "the amendment appended rather than updating"
        old, new = by_id[original_id], by_id[clone_id]

        # The old row stays queryable and keeps its own values.
        assert old.max_observation_age_seconds is None
        assert old.refresh_retry_policy is None
        assert old.superseded_by == clone_id

        # Exactly one active row.
        active = [r for r in rows if r.superseded_by is None]
        assert [r.id for r in active] == [clone_id]

        # The new values live only on the new row.
        assert new.max_observation_age_seconds == 900
        assert new.refresh_retry_policy["max_attempts"] == 3
        assert new.amendment_reason == "Week 0 capture freshness and refresh-retry policy freeze"

        # Nothing else moved.
        for field, value in before.items():
            assert getattr(new, field) == value, f"{field} changed"


def test_the_amendment_refuses_a_duplicate_rules_version():
    from app.services.amend_capture_policy import AmendmentRefused, apply_amendment

    game_id = _game("amend-dup")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        _rules(session, season_id, rules_version="2026-w0.1")

    with pytest.raises(AmendmentRefused, match="already the active version"):
        with session_scope() as session:
            apply_amendment(
                session, season_id=season_id,
                max_observation_age_seconds=900, refresh_retry_policy=V1_RETRY,
                rules_version="2026-w0.1", amendment_reason="x",
                effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
            )


def test_the_amendment_refuses_a_malformed_retry_policy_before_writing():
    import sqlalchemy

    from app.services.amend_capture_policy import apply_amendment

    game_id = _game("amend-bad")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        _rules(session, season_id, rules_version="2026-w0.1")

    with pytest.raises(CapturePolicyError):
        with session_scope() as session:
            apply_amendment(
                session, season_id=season_id,
                max_observation_age_seconds=900,
                refresh_retry_policy={"max_attempts": "3"},
                rules_version="2026-w0.2", amendment_reason="x",
                effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
            )

    with session_scope() as session:
        count = session.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(SeasonRules)
            .where(SeasonRules.season_id == season_id)
        ).scalar()
    assert count == 1, "a refused amendment wrote nothing"


def test_the_amendment_cannot_touch_methodology_fields():
    """The guard runs AFTER the clone is built, so a future edit that
    widened it would fail here rather than silently rewrite methodology."""

    from app.services import amend_capture_policy

    assert "starting_bankroll_cents" in amend_capture_policy.METHODOLOGY_FIELDS
    assert "kelly_fraction" in amend_capture_policy.METHODOLOGY_FIELDS
    assert "checkpoint_windows" in amend_capture_policy.METHODOLOGY_FIELDS
    assert set(amend_capture_policy.POLICY_FIELDS) == {
        "max_observation_age_seconds",
        "refresh_retry_policy",
    }
    assert not set(amend_capture_policy.POLICY_FIELDS) & set(
        amend_capture_policy.METHODOLOGY_FIELDS
    )


def test_the_dry_run_writes_nothing():
    import sqlalchemy

    from app.services.amend_capture_policy import plan_amendment

    game_id = _game("amend-dry")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        _rules(session, season_id, rules_version="2026-w0.1")

    with session_scope() as session:
        diff = plan_amendment(
            session, season_id=season_id,
            max_observation_age_seconds=900, refresh_retry_policy=V1_RETRY,
            rules_version="2026-w0.2", amendment_reason="freeze",
            effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        text = diff.render()

    assert "before" in text and "after" in text
    assert "2026-w0.1" in text and "2026-w0.2" in text

    with session_scope() as session:
        count = session.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(SeasonRules)
            .where(SeasonRules.season_id == season_id)
        ).scalar()
    assert count == 1


# =====================================================================
# 7. THE OFFICIAL RUNNER ACTUALLY REFRESHES
# =====================================================================
#
# The hole this section closes: official_capture.main() called
# run_official_checkpoint with no refresh, and run_checkpoint_cycle treats
# `refresh is None` as "no refresh supplied" and proceeds to the capture.
# Since a capture is irreversible, the shiny new official command would
# have consumed a real checkpoint on whatever stale quotes happened to be
# sitting in Postgres.


def _frozen_season(tag: str, *, provider=PROVIDER) -> uuid.UUID:
    """A game whose FINAL window is open against the REAL clock.

    The official runner deliberately exposes no `now_fn` -- an operator
    must not be able to tell an official capture what time it is -- so
    these tests move the kickoff instead of the clock.
    """

    game_id = _game(tag)
    with session_scope() as session:
        game = session.get(Game, game_id)
        # now + 4h puts now inside FINAL (kickoff-6h .. kickoff-2h).
        game.kickoff_at = datetime.now(timezone.utc) + timedelta(hours=4)
        session.flush()
        _rules(
            session, game.season_id,
            market_data_provider=provider,
            max_observation_age_seconds=900,
            refresh_retry_policy=V1_RETRY,
        )
    return game_id


def test_an_official_capture_without_a_wired_refresh_fails_closed():
    """A season pinned to a provider with no production refresh must never
    capture. "We could not fetch anything, so we froze whatever was lying
    around" is not a recoverable outcome -- it consumes the checkpoint."""

    import sqlalchemy

    from app.db.models.markets import CheckpointRun
    from app.marketdata.official_capture import NoProductionRefresh, run_official_checkpoint

    game_id = _frozen_season("no-refresh", provider="SOME_OTHER_PROVIDER")
    _market(game_id, "no-refresh")

    with pytest.raises(NoProductionRefresh):
        run_official_checkpoint(game_id=game_id, checkpoint_type="FINAL")

    with session_scope() as session:
        runs = session.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(CheckpointRun)
        ).scalar()
    assert runs == 0, "the official runner captured without refreshing"


def test_the_official_runner_refreshes_before_it_captures():
    """Order, not just presence. The capture must see the quotes the
    refresh wrote."""

    from app.marketdata.official_capture import run_official_checkpoint

    game_id = _frozen_season("refresh-first")
    market_id = _market(game_id, "refresh-first")
    order: list[str] = []

    def refresh() -> RefreshOutcome:
        order.append("refresh")
        _write_quote(market_id, sportsbook=CANONICAL, as_of_at=datetime.now(timezone.utc))
        return RefreshOutcome(
            ok=True,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            quotes_written=1,
            provider_calls=2,
        )

    report = run_official_checkpoint(
        game_id=game_id, checkpoint_type="FINAL", refresh=refresh,
        sleep_fn=lambda _s: None,
    )
    order.append("captured" if report.checkpoint_status == "CAPTURED" else report.checkpoint_status)

    assert order == ["refresh", "captured"]
    assert report.valid_baselines == 1, "the capture consumed the refresh's quotes"
    assert report.policy_is_official is True


def test_the_refresh_commits_before_the_capture_reads():
    """The refresh owns its own transactions. If it had not committed, the
    capture -- which opens a separate session -- would not see its rows."""

    from app.marketdata.official_capture import run_official_checkpoint

    game_id = _frozen_season("commits")
    market_id = _market(game_id, "commits")
    seen: dict[str, int] = {}

    def refresh() -> RefreshOutcome:
        _write_quote(market_id, sportsbook=CANONICAL, as_of_at=datetime.now(timezone.utc))
        # A SEPARATE session, exactly as the capture will use.
        import sqlalchemy

        from app.db.models.markets import PropQuote

        with session_scope() as other:
            seen["visible"] = other.execute(
                sqlalchemy.select(sqlalchemy.func.count()).select_from(PropQuote)
                .where(PropQuote.market_id == market_id)
            ).scalar()
        return RefreshOutcome(
            ok=True, started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc), quotes_written=1,
        )

    run_official_checkpoint(game_id=game_id, checkpoint_type="FINAL", refresh=refresh, sleep_fn=lambda _s: None)
    assert seen["visible"] == 1, "the refresh had not committed"


def test_a_failed_official_refresh_enters_the_frozen_retry_policy():
    """The retry budget comes from the rules, not from the runner."""

    from app.marketdata.official_capture import run_official_checkpoint

    game_id = _frozen_season("official-retry")
    _market(game_id, "official-retry")

    refresh = CountingRefresh(outcomes=[_fail("TIMEOUT")])
    report = run_official_checkpoint(
        game_id=game_id, checkpoint_type="FINAL", refresh=refresh,
        sleep_fn=lambda _s: None,
    )

    assert refresh.calls == V1_RETRY["max_attempts"]
    assert [a.waited_seconds for a in report.attempts] == [0.0, 30.0, 120.0]


def test_one_logical_attempt_reports_its_real_provider_call_count():
    """A logical refresh is several actual provider calls -- the nflverse
    roster and the Odds event odds -- so "one attempt == one ProviderCall"
    would understate what a retry costs."""

    from app.marketdata.official_capture import run_official_checkpoint

    game_id = _frozen_season("multi-call")
    _market(game_id, "multi-call")

    multi = RefreshOutcome(
        ok=False, started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        error="simulated", error_category="TIMEOUT",
        provider_calls=2, quota_cost=5,
    )
    refresh = CountingRefresh(outcomes=[multi])
    report = run_official_checkpoint(
        game_id=game_id, checkpoint_type="FINAL", refresh=refresh,
        sleep_fn=lambda _s: None,
    )

    assert refresh.calls == 3
    assert report.provider_calls_spent == 6, "2 calls x 3 attempts"
    assert report.quota_cost == 15


def test_the_official_cli_exposes_no_policy_or_event_overrides():
    import inspect

    from app.marketdata import official_capture

    source = inspect.getsource(official_capture)
    for flag in (
        "--max-observation-age-seconds", "--max-attempts", "--canonical-sportsbook",
        "--market-data-provider", "--event-id", "--retry-policy",
    ):
        assert flag not in source, f"{flag} must not be an official-capture option"

    parser_args = {"--game-id", "--checkpoint-type", "--dry-run"}
    for token in parser_args:
        assert token in source


def test_the_production_refresh_uses_the_persisted_game_identity():
    """No operator-supplied event id: Game.external_ref already holds the
    provider event identity, permanently."""

    from app.marketdata.game_refresh import event_ref_from_game

    game_id = _game("identity")
    with session_scope() as session:
        game = session.get(Game, game_id)
        game.external_ref = "THE_ODDS_API:abc123:withcolon"
        session.flush()
        ref = event_ref_from_game(game)

    assert ref.provider == "THE_ODDS_API"
    assert ref.external_event_id == "abc123:withcolon", "split on the FIRST colon only"


def test_the_production_refresh_builds_no_snapshot_or_evidence_and_calls_no_model():
    """Structural. The refresh's job ends at committed quotes; the capture
    clock and everything downstream belong to the capture."""

    import ast
    import inspect

    from app.marketdata import game_refresh

    tree = ast.parse(inspect.getsource(game_refresh))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in (
        "MarketSnapshotService", "build_snapshot", "create_evidence_snapshot",
        "capture_checkpoint", "AIOrchestrator", "list_events",
    ):
        assert forbidden not in code, f"the production refresh must not use {forbidden}"


def test_the_resolution_loop_has_one_implementation():
    """live_ingest and the production refresh share it. A second copy could
    drift -- different alias handling, a different quarantine rule -- and
    nothing would flag it, because both would keep producing plausible
    rows."""

    import ast
    import inspect

    from app.marketdata import live_ingest

    code = ast.unparse(ast.parse(inspect.getsource(live_ingest)))
    assert "persist_resolved_quotes" in code
    assert "resolve_and_record" not in code, "live_ingest re-implements identity resolution"
    assert "resolve_prop_market" not in code, "live_ingest re-implements market resolution"


def test_the_amendment_refuses_a_stale_parent():
    """The row the operator reviewed must be the row superseded. An
    amendment reviewed against one parent and applied against another is a
    silent methodology change."""

    from app.services.amend_capture_policy import AmendmentRefused, apply_amendment

    game_id = _game("stale-parent")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        _rules(session, season_id, rules_version="2026-research-v1")

    with pytest.raises(AmendmentRefused, match="expected the active rules"):
        with session_scope() as session:
            apply_amendment(
                session, season_id=season_id,
                max_observation_age_seconds=900, refresh_retry_policy=V1_RETRY,
                rules_version="2026-research-v2", amendment_reason="freeze",
                effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
                expected_current_version="some-other-version",
            )


def test_a_second_amendment_cannot_leave_two_active_rows():
    """Two amendment commands racing must not both supersede the same
    parent."""

    import sqlalchemy

    from app.services.amend_capture_policy import AmendmentRefused, apply_amendment

    game_id = _game("two-amendments")
    with session_scope() as session:
        game = session.get(Game, game_id)
        season_id = game.season_id
        _rules(session, season_id, rules_version="2026-research-v1")

    with session_scope() as session:
        apply_amendment(
            session, season_id=season_id,
            max_observation_age_seconds=900, refresh_retry_policy=V1_RETRY,
            rules_version="2026-research-v2", amendment_reason="freeze",
            effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
            expected_current_version="2026-research-v1",
        )

    # The second command still believes v1 is active -- it is not.
    with pytest.raises(AmendmentRefused):
        with session_scope() as session:
            apply_amendment(
                session, season_id=season_id,
                max_observation_age_seconds=600, refresh_retry_policy=V1_RETRY,
                rules_version="2026-research-v3", amendment_reason="freeze again",
                effective_from=datetime(2026, 9, 18, tzinfo=timezone.utc),
                expected_current_version="2026-research-v1",
            )

    with session_scope() as session:
        active = session.execute(
            sqlalchemy.select(SeasonRules)
            .where(SeasonRules.season_id == season_id, SeasonRules.superseded_by.is_(None))
        ).scalars().all()
    assert len(active) == 1
    assert active[0].rules_version == "2026-research-v2"
