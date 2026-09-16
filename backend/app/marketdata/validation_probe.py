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
    """The nine mandatory deliverables, plus the supporting counts."""

    event_id: str | None = None
    event_label: str | None = None
    kickoff_at: str | None = None
    vendor_market_keys_returned: list[str] = field(default_factory=list)
    vendor_market_keys_requested: list[str] = field(default_factory=list)
    draftkings_present: bool = False
    draftkings_families: dict[str, bool] = field(default_factory=dict)
    books_seen: list[str] = field(default_factory=list)
    player_identity: str = "UNKNOWN"
    player_team_available: bool = False
    player_position_available: bool = False
    alternate_line_shape: str = "UNDETERMINED"
    market_last_update: str | None = None
    last_update_level: str = "UNDETERMINED"
    quota_used: int | None = None
    quota_remaining: int | None = None
    quota_cost_total: int = 0
    raw_response_bytes_total: int = 0
    sample_quote: dict[str, Any] | None = None
    provider_call_ids: list[str] = field(default_factory=list)
    ingestion_run_id: str | None = None
    failures: list[str] = field(default_factory=list)


def _inspect_event_odds(decoded: Any, findings: ProbeFindings, family: StatFamily, key: str) -> None:
    """Read the vendor payload WITHOUT committing to a parser.

    Everything here is defensive `.get` on an unknown shape, because the
    shape is exactly what we are trying to learn. Findings are recorded as
    observations, never as assumptions.
    """

    if not isinstance(decoded, dict):
        findings.failures.append(f"{key}: event-odds payload was not a JSON object")
        return

    for bookmaker in decoded.get("bookmakers", []) or []:
        if not isinstance(bookmaker, dict):
            continue
        book_key = str(bookmaker.get("key", "")).upper()
        if book_key and book_key not in findings.books_seen:
            findings.books_seen.append(book_key)
        book_last_update = bookmaker.get("last_update")

        for market in bookmaker.get("markets", []) or []:
            if not isinstance(market, dict):
                continue
            market_key = str(market.get("key", ""))
            if market_key and market_key not in findings.vendor_market_keys_returned:
                findings.vendor_market_keys_returned.append(market_key)

            # DELIVERABLE 6: which level carries last_update?
            market_last_update = market.get("last_update")
            if market_last_update is not None:
                findings.market_last_update = str(market_last_update)
                findings.last_update_level = (
                    "MARKET_AND_BOOKMAKER" if book_last_update is not None else "MARKET"
                )
            elif book_last_update is not None and findings.last_update_level == "UNDETERMINED":
                findings.last_update_level = "BOOKMAKER_ONLY"

            outcomes = market.get("outcomes", []) or []
            if book_key == "DRAFTKINGS" and outcomes:
                findings.draftkings_present = True
                findings.draftkings_families[family.value] = True

            # DELIVERABLE 5: alternate lines within one mapped market?
            per_player_lines: dict[str, set] = {}
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                # DELIVERABLE 3/4: player identity and roster metadata.
                if outcome.get("description") is not None and findings.player_identity == "UNKNOWN":
                    findings.player_identity = "DISPLAY_NAME_ONLY (outcome.description)"
                for id_field in ("player_id", "participant_id", "participant", "id"):
                    if outcome.get(id_field) is not None:
                        findings.player_identity = f"STABLE_ID (outcome.{id_field})"
                        break
                if outcome.get("team") is not None:
                    findings.player_team_available = True
                if outcome.get("position") is not None:
                    findings.player_position_available = True

                who = str(outcome.get("description", ""))
                point = outcome.get("point")
                if who and point is not None:
                    per_player_lines.setdefault(who, set()).add(str(point))

                if findings.sample_quote is None and book_key == "DRAFTKINGS":
                    findings.sample_quote = {
                        "sportsbook": book_key,
                        "vendor_market_key": market_key,
                        "internal_stat_family": family.value,
                        "player_display_name": who,
                        "side": outcome.get("name"),
                        "line": None if point is None else str(point),
                        "american_price": outcome.get("price"),
                        "market_last_update": (
                            None if market_last_update is None else str(market_last_update)
                        ),
                        "bookmaker_last_update": (
                            None if book_last_update is None else str(book_last_update)
                        ),
                    }

            multi = {p: lines for p, lines in per_player_lines.items() if len(lines) > 1}
            if multi:
                findings.alternate_line_shape = (
                    "MULTIPLE_LINES_WITHIN_ONE_MARKET_KEY "
                    f"(e.g. {next(iter(multi))}: {sorted(next(iter(multi.values())))})"
                )
            elif findings.alternate_line_shape == "UNDETERMINED" and per_player_lines:
                findings.alternate_line_shape = "ONE_LINE_PER_PLAYER_IN_THIS_MARKET_KEY"


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

    # One call per family. Costs the same in total (1 credit per market per
    # region either way) but makes a bad candidate key diagnosable: a
    # combined request that 422s tells us nothing about WHICH spelling was
    # wrong.
    keys = tentative_market_keys()
    for vendor_key, family in keys.items():
        findings.vendor_market_keys_requested.append(vendor_key)
        result, decoded = provider.raw_event_odds(
            event_id=event.ref.external_event_id, market_keys=[vendor_key], sport=sport
        )
        with session_scope() as session:
            run = session.get(type(run), run_id)
            call = record_call(
                session,
                run=run,
                metadata=result.call_metadata,
                success=result.ok,
                error_category=result.error.category if result.error else None,
                error_message=result.error.message if result.error else None,
            )
            findings.provider_call_ids.append(str(call.id))
            findings.quota_cost_total += result.call_metadata.quota_cost or 0
            findings.raw_response_bytes_total += result.call_metadata.raw_response_bytes or 0
            if result.call_metadata.quota_remaining is not None:
                findings.quota_remaining = result.call_metadata.quota_remaining
                findings.quota_used = result.call_metadata.quota_used

        if not result.ok:
            findings.failures.append(
                f"{vendor_key}: {result.error.category}: {sanitize_message(result.error.message)}"
            )
            findings.draftkings_families.setdefault(family.value, False)
            continue
        findings.draftkings_families.setdefault(family.value, False)
        _inspect_event_odds(decoded, findings, family, vendor_key)

    usable = findings.event_id is not None and bool(findings.vendor_market_keys_returned)
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

    lines: list[str] = []
    add = lines.append
    add("=" * 70)
    add("THE ODDS API — PROVIDER VALIDATION PROBE")
    add("=" * 70)
    add(f"ingestion_run:  {findings.ingestion_run_id}")
    add(f"provider_calls: {len(findings.provider_call_ids)}")
    add(f"event:          {findings.event_label or '(none)'}  [{findings.event_id}]")
    add(f"kickoff:        {findings.kickoff_at}")
    add("")
    add("--- DELIVERABLE 1: vendor market keys -------------------------------")
    add(f"  requested: {', '.join(findings.vendor_market_keys_requested) or '(none)'}")
    add(f"  RETURNED:  {', '.join(findings.vendor_market_keys_returned) or '(none)'}")
    add("")
    add("--- DELIVERABLE 2: DraftKings coverage ------------------------------")
    add(f"  DRAFTKINGS present: {yn(findings.draftkings_present)}")
    for family, present in sorted(findings.draftkings_families.items()):
        add(f"    {family:24} {yn(present)}")
    add(f"  books seen ({len(findings.books_seen)}): {', '.join(findings.books_seen) or '(none)'}")
    add("")
    add("--- DELIVERABLE 3: player identity ----------------------------------")
    add(f"  {findings.player_identity}")
    add("")
    add("--- DELIVERABLE 4: roster metadata ----------------------------------")
    add(f"  team in payload:     {yn(findings.player_team_available)}")
    add(f"  position in payload: {yn(findings.player_position_available)}")
    if not (findings.player_team_available and findings.player_position_available):
        add("  NOTE: Player.team and Player.position are NOT NULL. If the provider")
        add("        supplies neither, production Week-0 ingestion is BLOCKED until")
        add("        a roster source or a schema decision exists. Placeholder values")
        add("        are prohibited.")
    add("")
    add("--- DELIVERABLE 5: alternate-line shape -----------------------------")
    add(f"  {findings.alternate_line_shape}")
    add("")
    add("--- DELIVERABLE 6: last_update binding level ------------------------")
    add(f"  level:  {findings.last_update_level}")
    add(f"  sample: {findings.market_last_update}")
    add("")
    add("--- DELIVERABLE 7: quota --------------------------------------------")
    add(f"  used:      {findings.quota_used}")
    add(f"  remaining: {findings.quota_remaining}")
    add(f"  cost of this probe: {findings.quota_cost_total}")
    add("")
    add("--- DELIVERABLE 8: raw response size --------------------------------")
    add(f"  {findings.raw_response_bytes_total} bytes across {len(findings.provider_call_ids)} calls")
    add("")
    add("--- DELIVERABLE 9: sample normalized quote --------------------------")
    if findings.sample_quote:
        for key, value in findings.sample_quote.items():
            add(f"  {key:24} {value}")
    else:
        add("  (no DraftKings outcome available to normalize)")
    add("")
    if findings.failures:
        add("--- FAILURES / DIAGNOSTICS ------------------------------------------")
        for failure in findings.failures:
            add(f"  {failure}")
        add("")
    add("Research tables (Game/Player/PropMarket/PropQuote/MarketSnapshot/")
    add("EvidenceSnapshot) were NOT written. This run recorded audit and quota")
    add("telemetry only.")
    add("")
    add("=" * 70)
    # Not "PASS": the probe answering its questions is not the same as the
    # answers being acceptable. Player identity, roster metadata and
    # alternate-line behaviour may each still require a human decision.
    add("PHASE 4 PROVIDER VALIDATION: COMPLETE — REVIEW REQUIRED")
    add("=" * 70)
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

    if findings.failures and not findings.vendor_market_keys_returned:
        return 1
    if findings.event_id is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
