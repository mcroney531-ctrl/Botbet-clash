"""Production-safe CLI smoke runner for Phase 3's live-provider proof.

Run as `python -m app.ai.live_smoke` from an environment that has real
DATABASE_URL/OPENAI_API_KEY/ANTHROPIC_API_KEY/GEMINI_API_KEY configured
(e.g. the deployed Railway service). Deliberately NOT a public HTTP
endpoint -- it makes real, billable provider calls, and pytest is a
dev-only dependency this module must not require, so it can't just be
`pytest -m live_provider` run in production.

This reuses AIOrchestrator, live_registry, the AgentSession lifecycle,
Forecast Lab's checkpoint/evidence machinery, validation, and the real
provider adapters exactly as built -- no forecasting or provider logic
is duplicated here. This module only builds one throwaway smoke-test
season/market/checkpoint, drives the existing orchestrator, and reports
a sanitized pass/fail summary: no API keys, no raw provider responses,
no environment variables.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.ai.orchestrator import AIOrchestrator
from app.ai.registry import live_registry
from app.ai.session_service import build_audit_receipt
from app.core.money import Money
from app.db.models.forecast_lab import AgentSession, AgentSessionEvidenceSnapshot, EvidenceSnapshot, ForecastObservation
from app.db.repositories.market_repository import MarketRepository
from app.db.repositories.season_repository import SeasonRepository
from app.db.session import session_scope
from app.domain.models import SeasonRules
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.services.season_commissioner import SeasonCommissioner

# Small/cheap model identifiers for the connectivity proof, deliberately
# not the season's eventual production roster (RULES.md's "known Week 0
# questions... roster/version" is explicitly not decided here) -- update
# if a provider retires one. This is the single canonical copy;
# app/tests/integration/test_live_providers.py imports it from here
# rather than keeping its own.
LIVE_MODEL_IDENTIFIERS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "google": "gemini-2.5-flash",
}

PROVIDER_LABELS = {"openai": "OPENAI", "anthropic": "ANTHROPIC", "google": "GOOGLE"}


def _smoke_rules() -> SeasonRules:
    return SeasonRules(
        rules_version="2026-live-smoke",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


@dataclass(frozen=True)
class SmokeFixture:
    commissioner: SeasonCommissioner
    season_competitor_ids: dict[str, uuid.UUID]
    checkpoint_run_id: uuid.UUID
    evidence_snapshot_id: uuid.UUID


def build_smoke_fixture() -> SmokeFixture:
    """One throwaway, non-real-money season: three competitors, one
    clean eligible market, one OPENING checkpoint, one frozen
    EvidenceSnapshot all three competitors will see identically."""

    commissioner = SeasonCommissioner.create_season(
        name=f"Phase 3 Live Smoke {uuid.uuid4()}", year=2026, rules=_smoke_rules()
    )
    with session_scope() as session:
        active_rules_row = SeasonRepository(session).get_active_rules(commissioner.season_id)
        windows_config = dict(active_rules_row.checkpoint_windows)
        canonical_book = active_rules_row.canonical_sportsbook

    season_competitor_ids: dict[str, uuid.UUID] = {}
    for provider_label in ("openai", "anthropic", "google"):
        sc_id = commissioner.register_competitor(
            competitor_id=provider_label, provider=provider_label, display_name=provider_label.upper(),
            model_identifier=LIVE_MODEL_IDENTIFIERS[provider_label], model_version="live-smoke",
        )
        season_competitor_ids[provider_label] = uuid.UUID(sc_id)

    commissioner.open_week(week_number=1, is_real_money=False)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=100)

    with session_scope() as session:
        market_repo = MarketRepository(session)
        game = market_repo.create_game(
            external_ref=f"live-smoke-{uuid.uuid4()}", season_id=commissioner.season_id,
            week_number=1, home_team="KC", away_team="BUF", kickoff_at=kickoff,
        )
        player = market_repo.create_player(
            external_ref=f"live-smoke-player-{uuid.uuid4()}", name="Player X", team="KC", position="WR"
        )
        market = market_repo.create_prop_market(game_id=game.id, player_id=player.id, stat_type="receiving_yards")
        market_repo.add_quote(
            market_id=market.id, sportsbook=canonical_book, line=Decimal("74.5"),
            over_price=-115, under_price=-105, retrieved_at=now - timedelta(hours=1),
        )
        game_id, market_id = game.id, market.id

    with session_scope() as session:
        game_row = MarketRepository(session).get_game(game_id)
        run = capture_checkpoint(
            session, game=game_row, checkpoint_type="OPENING", windows_config=windows_config,
            now=now, canonical_sportsbook=canonical_book,
        )
        evidence_id = session.execute(
            select(EvidenceSnapshot.id).where(
                EvidenceSnapshot.market_id == market_id, EvidenceSnapshot.checkpoint_type == "OPENING"
            )
        ).scalar_one()
        checkpoint_run_id = run.id

    return SmokeFixture(commissioner, season_competitor_ids, checkpoint_run_id, evidence_id)


@dataclass(frozen=True)
class SmokeVerification:
    provider: str
    session_id: uuid.UUID
    status: str
    probability_over: Decimal | None
    error_category: str | None
    error_message: str | None


@dataclass(frozen=True)
class SmokeChecks:
    shared_evidence: bool
    three_valid_sessions: bool
    three_forecast_observations: bool
    audit_receipts_valid: bool
    rendered_payload_equivalent: bool
    no_response_leakage: bool
    probabilities_in_range: bool

    @property
    def passed(self) -> bool:
        return all(
            [
                self.shared_evidence,
                self.three_valid_sessions,
                self.three_forecast_observations,
                self.audit_receipts_valid,
                self.rendered_payload_equivalent,
                self.no_response_leakage,
                self.probabilities_in_range,
            ]
        )


def format_probability(value: Decimal) -> str:
    """`.54321`, not `0.54321` -- matches the report's compact style."""

    text = f"{value:.5f}"
    return text[1:] if text.startswith("0.") else text


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _truncate(message: str | None, limit: int = 160) -> str | None:
    if message is None:
        return None
    return message if len(message) <= limit else message[: limit - 3] + "..."


def render_report(verifications: list[SmokeVerification], checks: SmokeChecks) -> str:
    """Pure formatting -- no DB/network access, safe to unit test with
    synthetic data."""

    lines: list[str] = []
    for v in verifications:
        label = PROVIDER_LABELS.get(v.provider, v.provider.upper())
        if v.status == "VALID" and v.probability_over is not None:
            lines.append(f"{label:<10} VALID   session={v.session_id}   p_over={format_probability(v.probability_over)}")
        else:
            detail = f"error_category={v.error_category}"
            truncated = _truncate(v.error_message)
            if truncated:
                detail += f"   error={truncated}"
            lines.append(f"{label:<10} {v.status:<7} session={v.session_id}   {detail}")

    lines.append("")
    lines.append(f"shared_evidence={_bool_str(checks.shared_evidence)}")
    lines.append(f"three_valid_sessions={_bool_str(checks.three_valid_sessions)}")
    lines.append(f"three_forecast_observations={_bool_str(checks.three_forecast_observations)}")
    lines.append(f"audit_receipts_valid={_bool_str(checks.audit_receipts_valid)}")
    lines.append(f"rendered_payload_equivalent={_bool_str(checks.rendered_payload_equivalent)}")
    lines.append(f"no_response_leakage={_bool_str(checks.no_response_leakage)}")
    lines.append(f"probabilities_in_range={_bool_str(checks.probabilities_in_range)}")
    lines.append("")
    lines.append(f"PHASE 3 LIVE PROVIDER PROOF: {'PASS' if checks.passed else 'FAIL'}")
    return "\n".join(lines)


def run_smoke(registry=None) -> tuple[str, bool]:
    """Drives one real three-model benchmark round through the existing
    orchestrator and returns (report_text, passed). The failed
    AgentSessions from any prior attempt are never touched -- each call
    here creates a brand-new season/competitors/checkpoint, so its
    orchestration_key is naturally unique and idempotency never confuses
    an old attempt with this one.

    `registry` defaults to `live_registry()` (real providers) -- the
    production CLI path via `main()` never overrides this. The parameter
    exists so the exact same verification/report logic below can be
    exercised end to end with `mock_registry()` in tests, without ever
    calling a real provider."""

    fixture = build_smoke_fixture()
    orchestrator = AIOrchestrator(season_id=fixture.commissioner.season_id, registry=registry or live_registry())
    session_ids = orchestrator.run_benchmark_round(
        season_competitor_ids=list(fixture.season_competitor_ids.values()),
        checkpoint_run_id=fixture.checkpoint_run_id,
        evidence_snapshot_ids=[fixture.evidence_snapshot_id],
    )

    verifications: list[SmokeVerification] = []
    rendered_by_provider: dict[str, dict | None] = {}
    evidence_ids_by_provider: dict[str, set[uuid.UUID]] = {}
    forecast_counts: dict[str, int] = {}
    audit_receipts_ok = True

    with session_scope() as session:
        for provider_label, sc_id in fixture.season_competitor_ids.items():
            agent_session_id = session_ids[sc_id]
            agent_session = session.get(AgentSession, agent_session_id)

            obs_rows = (
                session.query(ForecastObservation)
                .filter(ForecastObservation.agent_session_id == agent_session_id)
                .all()
            )
            forecast_counts[provider_label] = len(obs_rows)
            probability = obs_rows[0].model_probability_over if len(obs_rows) == 1 else None

            evidence_links = (
                session.query(AgentSessionEvidenceSnapshot)
                .filter(AgentSessionEvidenceSnapshot.agent_session_id == agent_session_id)
                .all()
            )
            evidence_ids_by_provider[provider_label] = {link.evidence_snapshot_id for link in evidence_links}
            rendered_by_provider[provider_label] = agent_session.rendered_request

            try:
                build_audit_receipt(session, agent_session_id)
            except Exception:
                audit_receipts_ok = False

            verifications.append(
                SmokeVerification(
                    provider=provider_label,
                    session_id=agent_session_id,
                    status=agent_session.status,
                    probability_over=probability,
                    error_category=agent_session.error_category,
                    error_message=agent_session.error_message,
                )
            )

    shared_evidence = (
        all(evidence_ids_by_provider[p] == {fixture.evidence_snapshot_id} for p in fixture.season_competitor_ids)
    )
    three_valid_sessions = all(v.status == "VALID" for v in verifications) and len(verifications) == 3
    three_forecast_observations = all(forecast_counts.get(p) == 1 for p in fixture.season_competitor_ids)

    rendered_users = [rendered_by_provider[p].get("user") if rendered_by_provider[p] else None for p in fixture.season_competitor_ids]
    rendered_payload_equivalent = all(u is not None for u in rendered_users) and all(u == rendered_users[0] for u in rendered_users)

    probabilities_in_range = all(
        Decimal("0") <= v.probability_over <= Decimal("1") for v in verifications if v.probability_over is not None
    )

    no_response_leakage = True
    valid_probabilities = {p: v.probability_over for p, v in zip(fixture.season_competitor_ids, verifications) if v.probability_over is not None}
    for provider_label, rendered in rendered_by_provider.items():
        if not rendered:
            continue
        payload_text = str(rendered.get("user"))
        for other_provider, other_prob in valid_probabilities.items():
            if other_provider == provider_label:
                continue
            if str(other_prob) in payload_text:
                no_response_leakage = False

    checks = SmokeChecks(
        shared_evidence=shared_evidence,
        three_valid_sessions=three_valid_sessions,
        three_forecast_observations=three_forecast_observations,
        audit_receipts_valid=audit_receipts_ok,
        rendered_payload_equivalent=rendered_payload_equivalent,
        no_response_leakage=no_response_leakage,
        probabilities_in_range=probabilities_in_range,
    )
    report = render_report(verifications, checks)
    return report, checks.passed


def main() -> int:
    report, passed = run_smoke()
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
