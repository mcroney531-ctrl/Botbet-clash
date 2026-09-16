"""Free-tier provider-validation probe.

    python -m app.marketdata.validation_probe

A production-safe CLI in the mould of app/ai/live_smoke.py: not pytest,
not an HTTP endpoint, no model calls, no money at risk beyond ~5 free
credits.

WHY THIS EXISTS
---------------
Five things about The Odds API's player-prop payload are unknown, and all
five change what the ingestion code should be:

  * the exact vendor market keys
  * whether a stable player identifier exists
  * whether team and position appear at all
  * whether alternate lines are separate keys or extra lines in one key
  * the level at which `last_update` is reported

Guessing any of them and writing a parser around the guess is precisely
what the seam document was written to prevent. So this probe discovers
the shape and REPORTS it. It deliberately does not persist research data:

  writes    ingestion_runs, provider_calls (incl. raw body + quota)
  does NOT  Game, Player, PropMarket, PropQuote, MarketSnapshot,
            EvidenceSnapshot

The first real provider call is for shape discovery, not for ingestion.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.marketdata.base import ProviderShapeReport
from app.marketdata.mapping import tentative_market_keys
from app.marketdata.providers.the_odds_api import (
    API_KEY_ENV_VAR,
    DEFAULT_SPORT,
    PROVIDER_NAME,
    TheOddsApiProvider,
)
from app.marketdata.telemetry import finish_run, record_call, sanitize_message, start_run

EVENT_WINDOW_DAYS = 8


@dataclass
class ProbeFindings:
    """The nine mandatory deliverables, plus supporting counts.

    Populated from the provider's neutral ProviderShapeReport. This module
    deliberately knows no vendor field names -- reading the payload is the
    adapter's job (seam doc §7).
    """

    event_id: str | None = None
    event_label: str | None = None
    kickoff_at: str | None = None
    vendor_market_keys_requested: list[str] = field(default_factory=list)
    shape: ProviderShapeReport | None = None
    quota_used: int | None = None
    quota_remaining: int | None = None
    quota_cost_total: int = 0
    raw_response_bytes_total: int = 0
    provider_call_ids: list[str] = field(default_factory=list)
    ingestion_run_id: str | None = None
    failures: list[str] = field(default_factory=list)


def run_probe(*, sport: str = DEFAULT_SPORT, persist_research: bool = False) -> ProbeFindings:
    findings = ProbeFindings()

    if persist_research:
        raise SystemExit(
            "Research persistence is not available in Phase 4A.1. The vendor's "
            "payload shape is still unverified, so writing Game/Player/PropMarket/"
            "PropQuote from it would be exactly the guess this probe exists to "
            "prevent. Re-run without --persist-research."
        )

    if not os.environ.get(API_KEY_ENV_VAR):
        raise SystemExit(
            f"{API_KEY_ENV_VAR} is not set in this environment.\n"
            "Set it in the Railway service Variables UI for the backend service, "
            "redeploy, then run this probe inside the container via `railway ssh`.\n"
            "Do not pass the key on a command line and do not paste it into a chat."
        )

    provider = TheOddsApiProvider(verified_only=False)
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        run = start_run(
            session,
            provider=PROVIDER_NAME,
            operation="VALIDATION_PROBE",
            sport=sport,
            requested_stat_families=[f.value for f in StatFamily],
            now=now,
        )
        findings.ingestion_run_id = str(run.id)
        run_id = run.id

    # No database transaction is held open across network I/O.
    events_result = provider.list_events(
        sport=sport, window_start=now, window_end=now + timedelta(days=EVENT_WINDOW_DAYS)
    )

    with session_scope() as session:
        run = session.get(type(run), run_id)
        call = record_call(
            session,
            run=run,
            metadata=events_result.call_metadata,
            success=events_result.ok,
            error_category=events_result.error.category if events_result.error else None,
            error_message=events_result.error.message if events_result.error else None,
        )
        findings.provider_call_ids.append(str(call.id))
        findings.quota_cost_total += events_result.call_metadata.quota_cost or 0
        findings.raw_response_bytes_total += events_result.call_metadata.raw_response_bytes or 0
        if events_result.call_metadata.quota_remaining is not None:
            findings.quota_remaining = events_result.call_metadata.quota_remaining
            findings.quota_used = events_result.call_metadata.quota_used
        if not events_result.ok:
            findings.failures.append(
                f"list_events: {events_result.error.category}: "
                f"{sanitize_message(events_result.error.message)}"
            )
            run.events_seen = 0
            finish_run(session, run=run, status="FAILED")
            return findings
        run.events_seen = len(events_result.payload)
        session.flush()

    if not events_result.payload:
        findings.failures.append(
            "list_events returned no upcoming NFL events in the next "
            f"{EVENT_WINDOW_DAYS} days; cannot probe a real event"
        )
        with session_scope() as session:
            finish_run(session, run=session.get(type(run), run_id), status="FAILED")
        return findings

    event = sorted(events_result.payload, key=lambda e: e.kickoff_at)[0]
    findings.event_id = event.ref.external_event_id
    findings.event_label = f"{event.away_team} @ {event.home_team}"
    findings.kickoff_at = event.kickoff_at.isoformat()

    # Shape discovery happens INSIDE the adapter and comes back neutral.
    # Costs one credit per market per region either way, but a per-family
    # call makes a bad candidate key individually diagnosable.
    findings.vendor_market_keys_requested = list(tentative_market_keys())
    results, shape = provider.discover_event_shape(
        event=event.ref, stat_families=list(StatFamily)
    )
    findings.shape = shape

    with session_scope() as session:
        run = session.get(type(run), run_id)
        for result in results:
            call = record_call(
                session,
                run=run,
                metadata=result.call_metadata,
                success=result.error is None,
                error_category=result.error.category if result.error else None,
                error_message=result.error.message if result.error else None,
                diagnostics=shape.diagnostics if result is results[-1] else (),
            )
            findings.provider_call_ids.append(str(call.id))
            findings.quota_cost_total += result.call_metadata.quota_cost or 0
            findings.raw_response_bytes_total += result.call_metadata.raw_response_bytes or 0
            if result.call_metadata.quota_remaining is not None:
                findings.quota_remaining = result.call_metadata.quota_remaining
                findings.quota_used = result.call_metadata.quota_used
            if result.error is not None:
                findings.failures.append(
                    f"{result.error.category}: {sanitize_message(result.error.message)}"
                )
        run.markets_quarantined = len(shape.diagnostics)
        session.flush()

    usable = findings.event_id is not None and bool(shape.market_keys_returned)
    with session_scope() as session:
        run = session.get(type(run), run_id)
        finish_run(
            session,
            run=run,
            status="SUCCEEDED" if usable and not findings.failures else "PARTIAL" if usable else "FAILED",
        )
    return findings


def render_report(findings: ProbeFindings) -> str:
    """Sanitized human report. Never prints raw response bodies."""

    def yn(value: bool) -> str:
        return "YES" if value else "NO"

    shape = findings.shape or ProviderShapeReport()

    lines: list[str] = []
    add = lines.append
    add("=" * 72)
    add("THE ODDS API — PROVIDER VALIDATION PROBE")
    add("=" * 72)
    add(f"ingestion_run:  {findings.ingestion_run_id}")
    add(f"provider_calls: {len(findings.provider_call_ids)}")
    add(f"event:          {findings.event_label or '(none)'}  [{findings.event_id}]")
    add(f"kickoff:        {findings.kickoff_at}")
    add("")
    add("--- DELIVERABLE 1: vendor market keys -------------------------------")
    add(f"  requested: {', '.join(findings.vendor_market_keys_requested) or '(none)'}")
    add(f"  RETURNED:  {', '.join(shape.market_keys_returned) or '(none)'}")
    add("")
    add("--- DELIVERABLE 2: DraftKings coverage ------------------------------")
    add(f"  DRAFTKINGS present: {yn(shape.canonical_book_present)}")
    quoted = set(shape.families_quoted_by_canonical)
    for family in StatFamily:
        add(f"    {family.value:24} {yn(family.value in quoted)}")
    add(f"  books seen ({len(shape.books_seen)}): {', '.join(shape.books_seen) or '(none)'}")
    add("")
    add("--- DELIVERABLE 3: player identity ----------------------------------")
    add(f"  {shape.player_identity}")
    if shape.player_identifier_field and not shape.player_identity_verified:
        add("  An identifier being PRESENT and an identifier MEANING player identity")
        add("  are different claims. Only the second may drive Player.external_ref,")
        add("  because a wrong guess silently merges or splits real people.")
    add("")
    add("--- DELIVERABLE 4: roster metadata ----------------------------------")
    add(f"  team in payload:     {yn(shape.team_available)}")
    add(f"  position in payload: {yn(shape.position_available)}")
    if not (shape.team_available and shape.position_available):
        add("  NOTE: Player.team and Player.position are NOT NULL. If the provider")
        add("        supplies neither, production Week-0 ingestion is BLOCKED until")
        add("        a roster source or a schema decision exists. Placeholder values")
        add("        are prohibited.")
    add("")
    add("--- DELIVERABLE 5: alternate-line shape -----------------------------")
    add(f"  {shape.alternate_line_shape}")
    add("  (Vendor docs describe alternates under separate `_alternate` market")
    add("   keys, which our mapping simply does not list. The ingestion-level")
    add("   ambiguity quarantine stays as defence in depth regardless.)")
    add("")
    add("--- DELIVERABLE 6: last_update binding level ------------------------")
    add(f"  levels present: {', '.join(shape.last_update_levels) or '(none)'}")
    add(f"  market-level sample: {shape.market_last_update_sample}")
    add("  provider_market_updated_at binds the MARKET-level value by design;")
    add("  a bookmaker-level value may also exist and is not what we persist.")
    add("")
    add("--- DELIVERABLE 7: quota --------------------------------------------")
    add(f"  used:      {findings.quota_used}")
    add(f"  remaining: {findings.quota_remaining}")
    add(f"  cost of this probe: {findings.quota_cost_total}")
    add("")
    add("--- DELIVERABLE 8: raw response size --------------------------------")
    add(
        f"  {findings.raw_response_bytes_total} bytes across "
        f"{len(findings.provider_call_ids)} calls"
    )
    add("")
    add("--- DELIVERABLE 9: one fully normalized ProviderQuote ---------------")
    if shape.sample_quote is not None:
        q = shape.sample_quote
        add(f"  sportsbook                 {q.sportsbook}")
        add(f"  player_display_name        {q.player.display_name}")
        add(f"  player_external_ref        {q.player.as_external_ref()}")
        add(f"  stat_family (internal)     {q.stat_family.value}")
        add(f"  vendor_market_key          {q.vendor_market_key}")
        add(f"  line                       {q.line}")
        add(f"  over_price                 {q.over_price}")
        add(f"  under_price                {q.under_price}")
        add(f"  as_of_at                   {q.as_of_at.isoformat()}")
        add(f"  retrieved_at               {q.retrieved_at.isoformat()}")
        add(
            "  provider_market_updated_at "
            f"{q.provider_market_updated_at.isoformat() if q.provider_market_updated_at else None}"
        )
        add(f"  source                     {q.source}")
        add("  (a matched Over/Under pair at one line — not a single outcome)")
    else:
        add("  NOT AVAILABLE — no normalized quote could be constructed.")
        add(f"  reason: {shape.sample_quote_unavailable_reason}")
        add("  Reported as unavailable rather than presenting one unpaired outcome")
        add("  as though it were a normalized quote.")
    add("")
    if shape.diagnostics:
        add("--- CONTENT DIAGNOSTICS / QUARANTINES -------------------------------")
        for diagnostic in shape.diagnostics:
            where = diagnostic.vendor_market_key or diagnostic.sportsbook or "-"
            add(f"  [{diagnostic.category}] {where}: {sanitize_message(diagnostic.detail)}")
        add("")
    if findings.failures:
        add("--- CALL FAILURES ---------------------------------------------------")
        for failure in findings.failures:
            add(f"  {failure}")
        add("")
    add("Research tables (Game/Player/PropMarket/PropQuote/MarketSnapshot/")
    add("EvidenceSnapshot) were NOT written. This run recorded audit and quota")
    add("telemetry only.")
    add("")
    add("=" * 72)
    # Not "PASS": the probe answering its questions is not the same as the
    # answers being acceptable. Player identity, roster metadata and
    # alternate-line behaviour may each still require a human decision.
    add("PHASE 4 PROVIDER VALIDATION: COMPLETE — REVIEW REQUIRED")
    add("=" * 72)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Odds API provider-validation probe")
    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument(
        "--persist-research",
        action="store_true",
        help="not available in Phase 4A.1; the probe never writes research tables",
    )
    args = parser.parse_args(argv)

    findings = run_probe(sport=args.sport, persist_research=args.persist_research)
    print(render_report(findings))

    if findings.failures and not (findings.shape and findings.shape.market_keys_returned):
        return 1
    if findings.event_id is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
