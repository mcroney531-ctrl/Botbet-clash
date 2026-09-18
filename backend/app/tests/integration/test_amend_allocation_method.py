"""The narrow, append-only allocation-methodology amendment.

Dedicated rather than a widened `amend_capture_policy`: "which fields may
this command change" is the entire safety property, and it weakens every
time a second kind of amendment is taught to the same tool.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.season import Season, SeasonRules
from app.db.session import session_scope
from app.services.amend_allocation_method import (
    FIELD,
    GUARDED_FIELDS,
    AmendmentRefused,
    apply_allocation_amendment,
    main,
    plan_allocation_amendment,
)
from app.services.amend_capture_policy import METHODOLOGY_FIELDS, POLICY_FIELDS

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
RETRY = {
    "max_attempts": 3, "backoff_seconds": [30.0, 120.0],
    "window_guard_seconds": 60.0, "request_budget_seconds": 180.0,
}


def _season(tag, *, version="2026-research-v2", method=None):
    with session_scope() as session:
        season = Season(year=2026, name=f"amend-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=version,
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider="THE_ODDS_API", roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards"], devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=10, benchmark_allocation_method=method,
            batch_methodology="SINGLE_BATCH",
            checkpoint_windows={
                "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
                "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
                "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
            },
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            max_observation_age_seconds=900, refresh_retry_policy=RETRY,
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        return season.id


def _active(season_id) -> SeasonRules:
    with session_scope() as session:
        return session.execute(
            select(SeasonRules).where(
                SeasonRules.season_id == season_id,
                SeasonRules.superseded_by.is_(None),
            )
        ).scalar_one()


REASON = "Week 0 benchmark fixture allocation methodology freeze"


def _amend(season_id, *, method="STABLE_HASH_V1", version="2026-research-v3",
           expected="2026-research-v2", reason=REASON):
    with session_scope() as session:
        return apply_allocation_amendment(
            session, season_id=season_id, allocation_method=method,
            new_rules_version=version, amendment_reason=reason,
            effective_from=NOW, expected_current_version=expected,
        )


def test_the_amendment_changes_exactly_one_column():
    season_id = _season("one-column")
    before = _active(season_id)
    snapshot = {f: getattr(before, f) for f in GUARDED_FIELDS}

    _amend(season_id)

    after = _active(season_id)
    assert after.rules_version == "2026-research-v3"
    assert getattr(after, FIELD) == "STABLE_HASH_V1"
    for field, value in snapshot.items():
        assert getattr(after, field) == value, f"{field} changed"


def test_the_capture_policy_survives_untouched():
    """The 900s freshness threshold and the retry policy were frozen by a
    separate amendment and must not move because a different one ran."""

    season_id = _season("policy-safe")
    _amend(season_id)
    after = _active(season_id)
    assert after.max_observation_age_seconds == 900
    assert after.refresh_retry_policy == RETRY


def test_the_guard_covers_every_field_both_amendments_know_about():
    """Derived, not retyped: a methodology column added to either list is
    guarded here without anyone remembering to copy it."""

    assert set(GUARDED_FIELDS) == (set(METHODOLOGY_FIELDS) | set(POLICY_FIELDS)) - {FIELD}
    assert FIELD not in GUARDED_FIELDS
    assert len(GUARDED_FIELDS) >= 23


def test_the_parent_is_superseded_not_modified():
    season_id = _season("append-only")
    before = _active(season_id)
    old_id, old_method = before.id, getattr(before, FIELD)

    diff = _amend(season_id)

    with session_scope() as session:
        parent = session.get(SeasonRules, old_id)
        assert parent.superseded_by == diff.new_rules_id
        assert getattr(parent, FIELD) == old_method, "the old row was rewritten"
        assert session.execute(
            select(func.count()).select_from(SeasonRules)
            .where(SeasonRules.season_id == season_id)
        ).scalar() == 2


def test_an_unapproved_method_is_refused():
    season_id = _season("unapproved")
    with pytest.raises(AmendmentRefused, match="NOT APPROVED"):
        _amend(season_id, method="KICKOFF_BLOCK_STRATIFIED_V1")
    assert getattr(_active(season_id), FIELD) is None


def test_the_rejected_v0_is_refused():
    season_id = _season("v0")
    with pytest.raises(AmendmentRefused, match="NOT APPROVED"):
        _amend(season_id, method="ROUND_ROBIN_BY_KICKOFF_V0")


def test_an_unimplemented_method_is_refused():
    season_id = _season("unknown")
    with pytest.raises(AmendmentRefused, match="not an implemented allocator"):
        _amend(season_id, method="LOOKS_GOOD_V9")


def test_a_stale_expected_parent_refuses():
    season_id = _season("stale")
    with pytest.raises(AmendmentRefused, match="expected the active rules"):
        _amend(season_id, expected="2026-research-v1")
    assert _active(season_id).rules_version == "2026-research-v2"


def test_a_no_op_amendment_is_refused():
    season_id = _season("noop", method="STABLE_HASH_V1")
    with pytest.raises(AmendmentRefused, match="nothing to amend"):
        _amend(season_id)


def test_reusing_the_active_version_is_refused():
    season_id = _season("same-version")
    with pytest.raises(AmendmentRefused, match="appends a new version"):
        _amend(season_id, version="2026-research-v2")


def test_the_apply_revalidates_the_parent_under_a_lock():
    """The dry run and the apply are separate processes. An amendment that
    landed between them must not be silently superseded by this one."""

    season_id = _season("locked")
    with session_scope() as session:
        plan_allocation_amendment(
            session, season_id=season_id, allocation_method="STABLE_HASH_V1",
            new_rules_version="2026-research-v3", amendment_reason=REASON,
            effective_from=NOW, expected_current_version="2026-research-v2",
        )
    # Somebody else amends first.
    with session_scope() as session:
        rules = session.execute(
            select(SeasonRules).where(SeasonRules.season_id == season_id)
        ).scalar_one()
        rules.rules_version = "2026-research-v2b"

    with pytest.raises(AmendmentRefused, match="expected the active rules"):
        _amend(season_id)
    assert _active(season_id).rules_version == "2026-research-v2b"


def test_the_under_lock_recheck_catches_a_race_with_no_expected_version():
    """`--expect-current-version` catches the common case, but it is
    optional at the service layer. The under-lock comparison against the
    PLANNED parent is what covers a caller that omits it."""

    import app.services.amend_allocation_method as module

    season_id = _season("no-expected")
    original = module.plan_allocation_amendment

    def amend_after_plan(session, **kwargs):
        diff = original(session, **kwargs)
        # Another amendment lands between plan and write.
        with session_scope() as other:
            rules = other.execute(
                select(SeasonRules).where(SeasonRules.season_id == season_id)
            ).scalar_one()
            rules.rules_version = "2026-research-v2b"
        return diff

    module.plan_allocation_amendment = amend_after_plan
    try:
        with pytest.raises(AmendmentRefused, match="moved while this amendment"):
            with session_scope() as session:
                apply_allocation_amendment(
                    session, season_id=season_id,
                    allocation_method="STABLE_HASH_V1",
                    new_rules_version="2026-research-v3",
                    amendment_reason=REASON,
                    effective_from=NOW,
                    expected_current_version=None,
                )
    finally:
        module.plan_allocation_amendment = original

    assert _active(season_id).rules_version == "2026-research-v2b"
    assert getattr(_active(season_id), FIELD) is None


def test_the_apply_takes_the_parent_for_update():
    import ast
    import inspect

    from app.services import amend_allocation_method

    source = inspect.getsource(amend_allocation_method.apply_allocation_amendment)
    tree = ast.parse(source.lstrip())
    kwargs = {
        kw.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
        for kw in node.keywords
    }
    assert "lock" in kwargs, "apply reads the parent without locking it"


def test_the_amendment_reaches_no_provider_and_no_network():
    import ast
    import inspect

    from app.services import amend_allocation_method

    tree = ast.parse(inspect.getsource(amend_allocation_method))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("fetch_schedule", "list_events", "fetch_quotes",
                      "TheOddsApiProvider", "requests", "httpx"):
        assert forbidden not in names, f"the amendment reaches {forbidden}"


def test_the_dry_run_writes_nothing(capsys):
    season_id = _season("cli-dry")
    code = main([
        "--season-id", str(season_id),
        "--allocation-method", "STABLE_HASH_V1",
        "--new-rules-version", "2026-research-v3",
        "--reason", REASON,
        "--expect-current-version", "2026-research-v2",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert "2026-research-v2  ->  2026-research-v3" in out
    assert _active(season_id).rules_version == "2026-research-v2"
    assert getattr(_active(season_id), FIELD) is None


def test_the_dry_run_names_the_rejected_methods_and_why(capsys):
    season_id = _season("cli-why")
    main([
        "--season-id", str(season_id),
        "--allocation-method", "STABLE_HASH_V1",
        "--new-rules-version", "2026-research-v3",
        "--reason", REASON,
        "--expect-current-version", "2026-research-v2",
    ])
    out = capsys.readouterr().out
    assert "KICKOFF_BLOCK_STRATIFIED_V1" in out
    assert "ROUND_ROBIN_BY_KICKOFF_V0" in out
    assert "STRATIFIED_BY_KICKOFF_V1" in out
    # The V0 rejection must state the bias at the PRODUCTION slot count, not
    # at the five-slot regime the first review ran under.
    assert "2 of 6 kickoff blocks" in out
    assert "Monday-night" in out


def test_the_cli_refuses_cleanly(capsys):
    season_id = _season("cli-refuse")
    code = main([
        "--season-id", str(season_id),
        "--allocation-method", "KICKOFF_BLOCK_STRATIFIED_V1",
        "--new-rules-version", "2026-research-v3",
        "--reason", REASON,
        "--expect-current-version", "2026-research-v2",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    assert "Traceback" not in out


def test_the_amended_season_can_then_commit_an_official_slate():
    """The whole point: the official path refuses on NULL and stops
    refusing once a reviewed method is frozen."""

    from app.forecast_lab.official_slate import MethodologyNotFrozen, propose_slate
    from app.tests.integration.test_official_slate import IN_TIME, StubSchedule

    season_id = _season("then-commit")
    with pytest.raises(MethodologyNotFrozen):
        propose_slate(season_id=season_id, week_number=3,
                      schedule_provider=StubSchedule(), now=IN_TIME)

    _amend(season_id)

    proposal = propose_slate(
        season_id=season_id, week_number=3,
        schedule_provider=StubSchedule(), now=IN_TIME,
    )
    assert proposal.allocation_method == "STABLE_HASH_V1"
    assert len(proposal.chosen) == 10


def test_the_amendment_requires_a_reason_and_persists_it():
    """A rules row that cannot explain itself is the audit gap the
    append-only lineage exists to close."""

    season_id = _season("reasoned")
    with pytest.raises(AmendmentRefused, match="must say why"):
        _amend(season_id, reason="   ")

    _amend(season_id)
    assert _active(season_id).amendment_reason == REASON


def test_the_reason_appears_in_the_diff(capsys):
    season_id = _season("reason-shown")
    main([
        "--season-id", str(season_id),
        "--allocation-method", "STABLE_HASH_V1",
        "--new-rules-version", "2026-research-v3",
        "--reason", REASON,
        "--expect-current-version", "2026-research-v2",
    ])
    assert REASON in capsys.readouterr().out
