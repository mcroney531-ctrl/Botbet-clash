"""Pure (no-DB, no-provider) tests for the live_smoke CLI's report
formatting -- the orchestration itself is already covered end to end by
the mock-path AIOrchestrator tests; this only exercises the
report-building logic in isolation with synthetic data."""

import uuid
from decimal import Decimal

from app.ai.live_smoke import SmokeChecks, SmokeVerification, format_probability, render_report

ALL_TRUE_CHECKS = SmokeChecks(
    shared_evidence=True,
    three_valid_sessions=True,
    three_forecast_observations=True,
    audit_receipts_valid=True,
    rendered_payload_equivalent=True,
    no_response_leakage=True,
    probabilities_in_range=True,
)


def _valid(provider: str, probability: str) -> SmokeVerification:
    return SmokeVerification(
        provider=provider, session_id=uuid.uuid4(), status="VALID",
        probability_over=Decimal(probability), error_category=None, error_message=None,
    )


def test_format_probability_strips_leading_zero():
    assert format_probability(Decimal("0.54321")) == ".54321"
    assert format_probability(Decimal("1.00000")) == "1.00000"
    assert format_probability(Decimal("0")) == ".00000"


def test_render_report_passes_when_all_checks_true():
    verifications = [_valid("openai", "0.54321"), _valid("anthropic", "0.51780"), _valid("google", "0.56100")]
    report = render_report(verifications, ALL_TRUE_CHECKS)

    assert "OPENAI     VALID" in report
    assert "p_over=.54321" in report
    assert "ANTHROPIC  VALID" in report
    assert "GOOGLE     VALID" in report
    assert "shared_evidence=true" in report
    assert "PHASE 3 LIVE PROVIDER PROOF: PASS" in report


def test_render_report_fails_when_any_check_false():
    verifications = [_valid("openai", "0.6"), _valid("anthropic", "0.5"), _valid("google", "0.55")]
    checks = SmokeChecks(**{**ALL_TRUE_CHECKS.__dict__, "audit_receipts_valid": False})
    report = render_report(verifications, checks)

    assert "audit_receipts_valid=false" in report
    assert "PHASE 3 LIVE PROVIDER PROOF: FAIL" in report


def test_render_report_shows_failure_detail_without_valid_probability():
    failed = SmokeVerification(
        provider="google", session_id=uuid.uuid4(), status="FAILED",
        probability_over=None, error_category="TIMEOUT", error_message="connection timed out",
    )
    verifications = [_valid("openai", "0.6"), _valid("anthropic", "0.5"), failed]
    checks = SmokeChecks(**{**ALL_TRUE_CHECKS.__dict__, "three_valid_sessions": False})
    report = render_report(verifications, checks)

    assert "GOOGLE     FAILED" in report
    assert "error_category=TIMEOUT" in report
    assert "error=connection timed out" in report
    assert "PHASE 3 LIVE PROVIDER PROOF: FAIL" in report


def test_render_report_never_prints_full_giant_error_message():
    huge_message = "x" * 5000
    failed = SmokeVerification(
        provider="openai", session_id=uuid.uuid4(), status="INVALID",
        probability_over=None, error_category="SCHEMA_VALIDATION_FAILED", error_message=huge_message,
    )
    checks = SmokeChecks(**{**ALL_TRUE_CHECKS.__dict__, "three_valid_sessions": False})
    report = render_report([failed], checks)

    assert huge_message not in report
    assert "..." in report
