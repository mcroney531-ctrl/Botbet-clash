"""Proves app/ai/live_smoke.py's full pipeline -- fixture build ->
orchestrator run -> DB verification -> report rendering -- end to end
against real Postgres, using MockAdapter instead of real providers. This
is the cheap insurance for the CLI GPT asked for: if this fails, the
Railway run against real providers would fail on the exact same logic
and burn real API calls finding that out instead.
"""

import app.ai.live_smoke as live_smoke_module
from app.ai.live_smoke import build_smoke_fixture, run_smoke
from app.ai.providers.mock import MockOutcome
from app.ai.registry import mock_registry
from app.db.models.forecast_lab import EvidenceSnapshot
from app.db.session import session_scope


def _registry_for(fixture, *, call_plans_by_provider=None):
    with session_scope() as session:
        evidence = session.get(EvidenceSnapshot, fixture.evidence_snapshot_id)
        market_id_str = str(evidence.market_id)
    return mock_registry(
        fixtures_by_provider={
            "openai": {market_id_str: 0.6},
            "anthropic": {market_id_str: 0.5},
            "google": {market_id_str: 0.55},
        },
        call_plans_by_provider=call_plans_by_provider,
    )


def _run_smoke_with_fixture(fixture, registry):
    """run_smoke() builds its own fixture internally; swap that call to
    reuse the exact fixture our mock registry's market_id was built
    from, so the two stay consistent."""

    original_build = live_smoke_module.build_smoke_fixture
    live_smoke_module.build_smoke_fixture = lambda: fixture
    try:
        return run_smoke(registry=registry)
    finally:
        live_smoke_module.build_smoke_fixture = original_build


def test_run_smoke_full_pipeline_passes_with_mock_registry():
    """The real end-to-end proof: call run_smoke() itself -- the exact
    function main() calls -- with a mock registry, and confirm every
    check it reports comes back true."""

    fixture = build_smoke_fixture()
    registry = _registry_for(fixture)

    report, passed = _run_smoke_with_fixture(fixture, registry)

    assert passed is True
    assert "PHASE 3 LIVE PROVIDER PROOF: PASS" in report
    assert "OPENAI     VALID" in report
    assert "ANTHROPIC  VALID" in report
    assert "GOOGLE     VALID" in report
    assert "shared_evidence=true" in report
    assert "three_valid_sessions=true" in report
    assert "three_forecast_observations=true" in report
    assert "audit_receipts_valid=true" in report
    assert "rendered_payload_equivalent=true" in report
    assert "no_response_leakage=true" in report
    assert "probabilities_in_range=true" in report


def test_run_smoke_reports_fail_when_a_provider_times_out():
    fixture = build_smoke_fixture()
    registry = _registry_for(fixture, call_plans_by_provider={"google": [MockOutcome.timeout()]})

    report, passed = _run_smoke_with_fixture(fixture, registry)

    assert passed is False
    assert "PHASE 3 LIVE PROVIDER PROOF: FAIL" in report
    assert "GOOGLE     FAILED" in report
    assert "error_category=TIMEOUT" in report
    assert "OPENAI     VALID" in report  # the other two still succeed independently
