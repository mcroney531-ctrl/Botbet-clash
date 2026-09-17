"""Pure unit tests for the roster/identity seam. No database, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.rosterdata.base import LIVE_ROSTER_MAX_AGE_HOURS, RosterEntry, RosterSnapshot
from app.rosterdata.resolution import (
    RESOLVER_VERSION,
    TeamGameMismatch,
    derive_opponent,
    resolve_player,
    stable_player_external_ref,
)
from app.rosterdata.teams import (
    ODDS_API_TEAM_NAMES,
    CanonicalTeam,
    TeamMappingError,
    canonical_from_nflverse,
    canonical_from_odds_api,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _snapshot(*entries, week: int | None = 2, retrieved_at: datetime = NOW) -> RosterSnapshot:
    return RosterSnapshot(
        provider="NFLVERSE",
        season=2026,
        week=week,
        basis="CURRENT_CONTEMPORANEOUS",
        entries=entries,
        retrieved_at=retrieved_at,
    )


GOFF = RosterEntry("00-0033106", "Jared Goff", CanonicalTeam.DET, "QB", 2026, 2)
ALLEN = RosterEntry("00-0034857", "Josh Allen", CanonicalTeam.BUF, "QB", 2026, 2)


# --- team vocabulary --------------------------------------------------


def test_every_team_is_mapped_exactly_once_from_each_provider():
    assert len(CanonicalTeam) == 32
    assert len(ODDS_API_TEAM_NAMES) == 32
    assert len(set(ODDS_API_TEAM_NAMES.values())) == 32, "no two names may share a team"


def test_nflverse_uses_LA_not_LAR():
    """Observed in the real 2026 roster file. Recalling `LAR` here would
    silently drop every Rams player from resolution."""

    assert canonical_from_nflverse("LA") is CanonicalTeam.LA
    assert canonical_from_odds_api("Los Angeles Rams") is CanonicalTeam.LA
    with pytest.raises(TeamMappingError):
        canonical_from_nflverse("LAR")


def test_unmapped_team_strings_raise_rather_than_being_guessed():
    for bad in ("Oakland Raiders", "Buffalo", "bills", "St. Louis Rams"):
        with pytest.raises(TeamMappingError, match="never resolved by guessing|explicit mapping"):
            canonical_from_odds_api(bad)


# --- the opponent fix -------------------------------------------------


def test_opponent_derivation_is_an_explicit_three_way_branch():
    assert derive_opponent(
        team=CanonicalTeam.DET, home_team=CanonicalTeam.BUF, away_team=CanonicalTeam.DET
    ) is CanonicalTeam.BUF
    assert derive_opponent(
        team=CanonicalTeam.BUF, home_team=CanonicalTeam.BUF, away_team=CanonicalTeam.DET
    ) is CanonicalTeam.DET


def test_a_team_in_neither_side_raises_instead_of_guessing():
    """The regression this seam exists for.

    The old code was `away if team == home else home`, so a team matching
    neither side silently produced the HOME team — telling half the slate
    it faced itself. There is no `else` branch any more.
    """

    with pytest.raises(TeamGameMismatch, match="refusing to guess"):
        derive_opponent(
            team=CanonicalTeam.KC, home_team=CanonicalTeam.BUF, away_team=CanonicalTeam.DET
        )


def test_the_old_broken_comparison_is_gone_from_the_orchestrator():
    import inspect

    from app.ai import orchestrator

    # Comment lines are stripped, because the fixed code deliberately QUOTES
    # the old expression in an explanatory comment -- the point is that it no
    # longer executes, not that the string never appears.
    code_only = "\n".join(
        line for line in inspect.getsource(orchestrator).splitlines()
        if not line.strip().startswith("#")
    )
    assert "player.team == game.home_team" not in code_only
    assert "derive_opponent(" in code_only


# --- resolution -------------------------------------------------------


def test_resolution_returns_identity_team_position_and_opponent():
    r = resolve_player(
        odds_display_name="Jared Goff",
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=_snapshot(GOFF, ALLEN),
    )
    assert r.outcome == "RESOLVED"
    assert r.stable_id == "00-0033106"
    assert r.team is CanonicalTeam.DET
    assert r.position == "QB"
    assert r.opponent is CanonicalTeam.BUF
    assert stable_player_external_ref(r.stable_id) == "GSIS:00-0033106"


def test_identity_is_gsis_not_the_resolving_vendor():
    """nflverse RESOLVES the identity; GSIS IS it. A vendor-scoped ref
    would orphan every Player row if the roster provider changed."""

    assert stable_player_external_ref("00-0033106").startswith("GSIS:")
    assert "NFLVERSE" not in stable_player_external_ref("00-0033106")


def test_a_name_on_neither_team_is_unresolved_not_matched_globally():
    kelce = RosterEntry("00-0030506", "Travis Kelce", CanonicalTeam.KC, "TE", 2026, 2)
    r = resolve_player(
        odds_display_name="Travis Kelce",
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=_snapshot(GOFF, ALLEN, kelce),
    )
    assert r.outcome == "UNRESOLVED_PLAYER"


def test_two_same_named_players_on_the_two_teams_are_ambiguous_not_arbitrary():
    """Measured: four duplicate normalized names existed league-wide in a
    single 2026 week. Within one game it is rarer, but the resolver must
    refuse rather than pick."""

    a = RosterEntry("00-0000001", "Byron Young", CanonicalTeam.DET, "LB", 2026, 2)
    b = RosterEntry("00-0000002", "Byron Young", CanonicalTeam.BUF, "DE", 2026, 2)
    r = resolve_player(
        odds_display_name="Byron Young",
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=_snapshot(a, b),
    )
    assert r.outcome == "AMBIGUOUS_PLAYER"
    assert "refusing to choose between real people" in r.detail


def test_normalization_is_conservative_in_both_directions():
    snap = _snapshot(GOFF, ALLEN)

    def outcome(name):
        return resolve_player(
            odds_display_name=name,
            home_team=CanonicalTeam.BUF,
            away_team=CanonicalTeam.DET,
            snapshot=snap,
        ).outcome

    assert outcome("  jared   GOFF ") == "RESOLVED"        # casing/whitespace ok
    assert outcome("Jared Goff Jr.") == "UNRESOLVED_PLAYER"  # suffix NOT stripped
    assert outcome("J. Goff") == "UNRESOLVED_PLAYER"         # no fuzzy matching


def test_a_roster_entry_without_a_stable_id_is_refused():
    """Observed: the real 2026 roster contains a row with no gsis_id."""

    nameless = RosterEntry("", "Some Player", CanonicalTeam.DET, "WR", 2026, 2)
    r = resolve_player(
        odds_display_name="Some Player",
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=_snapshot(nameless),
    )
    assert r.outcome == "MISSING_STABLE_ID"


def test_missing_position_does_not_block_resolution():
    no_pos = RosterEntry("00-0033106", "Jared Goff", CanonicalTeam.DET, None, 2026, 2)
    r = resolve_player(
        odds_display_name="Jared Goff",
        home_team=CanonicalTeam.BUF,
        away_team=CanonicalTeam.DET,
        snapshot=_snapshot(no_pos),
    )
    assert r.outcome == "RESOLVED"
    assert r.position is None


# --- freshness --------------------------------------------------------


def test_snapshot_age_supports_the_thirty_six_hour_live_tolerance():
    assert LIVE_ROSTER_MAX_AGE_HOURS == 36
    stale = _snapshot(GOFF, retrieved_at=NOW - timedelta(hours=40))
    fresh = _snapshot(GOFF, retrieved_at=NOW - timedelta(hours=5))
    assert stale.age_hours(at=NOW) > LIVE_ROSTER_MAX_AGE_HOURS
    assert fresh.age_hours(at=NOW) <= LIVE_ROSTER_MAX_AGE_HOURS


def test_resolver_version_is_recorded_for_provenance():
    assert RESOLVER_VERSION


# --- provider parsing (no network) ------------------------------------


def _csv(rows: str) -> bytes:
    return rows.encode()


def test_provider_rejects_a_roster_missing_an_expected_column():
    import httpx

    from app.rosterdata.providers.nflverse import NflverseRosterProvider

    body = _csv("season,team,position,gsis_id\n2026,DET,QB,00-0033106\n")  # no full_name
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    result = NflverseRosterProvider(client=client).fetch_current_roster(season=2026)
    assert result.error.category == "MALFORMED_ROSTER_RESPONSE"
    assert "full_name" in result.error.message


def test_provider_quarantines_an_unknown_team_code_without_failing_the_load():
    import httpx

    from app.rosterdata.providers.nflverse import NflverseRosterProvider

    body = _csv(
        "season,week,team,position,full_name,gsis_id\n"
        "2026,2,DET,QB,Jared Goff,00-0033106\n"
        "2026,2,XXX,QB,Nobody Atall,00-0000000\n"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    result = NflverseRosterProvider(client=client).fetch_current_roster(season=2026)
    assert result.ok
    assert [e.display_name for e in result.payload.entries] == ["Jared Goff"]


def test_weekly_and_current_products_carry_different_evidentiary_basis():
    import httpx

    from app.rosterdata.providers.nflverse import NflverseRosterProvider

    body = _csv(
        "season,week,team,position,full_name,gsis_id\n2026,2,DET,QB,Jared Goff,00-0033106\n"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    provider = NflverseRosterProvider(client=client)
    assert provider.fetch_current_roster(season=2026).payload.basis == "CURRENT_CONTEMPORANEOUS"
    assert (
        provider.fetch_weekly_roster(season=2026, week=2).payload.basis
        == "HISTORICAL_RECONSTRUCTED"
    ), "today's weekly files are not proof of what was available at an old checkpoint"


def test_raw_bytes_and_hash_are_retained_for_provenance():
    import httpx

    from app.marketdata.telemetry import sha256_hex
    from app.rosterdata.providers.nflverse import NflverseRosterProvider

    body = _csv(
        "season,week,team,position,full_name,gsis_id\n2026,2,DET,QB,Jared Goff,00-0033106\n"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    result = NflverseRosterProvider(client=client).fetch_current_roster(season=2026)
    assert result.call_metadata.raw_response_body == body
    assert result.call_metadata.raw_response_sha256 == sha256_hex(body)
    assert result.call_metadata.raw_response_bytes == len(body)


# --- freshness must be measured at the point of use --------------------


def test_a_roster_fetched_after_run_start_reports_a_non_negative_age():
    """The acceptance-report clock bug.

    live_ingest read `now` before the nflverse download, while
    RosterSnapshot.retrieved_at is stamped post-response. Evaluating
    age_hours(at=now) is therefore (earlier - later): negative, and MOST
    negative for the freshest possible roster. The run meant to prove the
    36-hour policy would have reported nonsense.
    """

    run_started = NOW
    fetched_at = NOW + timedelta(seconds=90)      # download took 90s
    snapshot = _snapshot(GOFF, retrieved_at=fetched_at)

    # The bug, stated explicitly so it cannot quietly come back.
    assert snapshot.age_hours(at=run_started) < 0

    # Measured at the point of use, after the fetch.
    checked_at = fetched_at + timedelta(seconds=2)
    age = snapshot.age_hours(at=checked_at)
    assert age >= 0
    assert age < LIVE_ROSTER_MAX_AGE_HOURS


def test_live_ingest_measures_roster_age_after_the_fetch_not_at_run_start():
    import inspect

    from app.marketdata import live_ingest

    code = "\n".join(
        line for line in inspect.getsource(live_ingest.run).splitlines()
        if not line.strip().startswith("#")
    )
    assert "age_hours(at=now)" not in code
    assert "roster_checked_at" in code


def test_a_genuinely_stale_snapshot_still_exceeds_the_tolerance():
    """The guard must not become permissive in fixing the sign."""

    checked_at = NOW
    stale = _snapshot(GOFF, retrieved_at=NOW - timedelta(hours=37))
    assert stale.age_hours(at=checked_at) > LIVE_ROSTER_MAX_AGE_HOURS


# --- explicit alias: narrow by construction ----------------------------

PALMER = RosterEntry("00-0036988", "Josh Palmer", CanonicalTeam.BUF, "WR", 2026, None)
ODDS = "THE_ODDS_API"


def _resolve(name, snapshot, provider=ODDS, home=CanonicalTeam.BUF, away=CanonicalTeam.DET):
    return resolve_player(
        odds_display_name=name,
        home_team=home,
        away_team=away,
        snapshot=snapshot,
        provider=provider,
    )


def test_the_measured_alias_resolves_to_the_stable_identity():
    """Measured 2026-09-17: Odds API "Joshua Palmer" vs nflverse
    "Josh Palmer". One miss in fifteen players, 11 quotes lost."""

    r = _resolve("Joshua Palmer", _snapshot(PALMER, ALLEN))
    assert r.outcome == "RESOLVED"
    assert r.stable_id == "00-0036988"
    assert r.team is CanonicalTeam.BUF
    assert r.opponent is CanonicalTeam.DET
    assert "explicit alias" in r.detail


def test_exact_matching_still_wins_before_the_alias_is_consulted():
    r = _resolve("Josh Palmer", _snapshot(PALMER, ALLEN))
    assert r.outcome == "RESOLVED"
    assert r.stable_id == "00-0036988"
    assert r.detail == "", "an exact hit must not be attributed to the alias pass"


def test_josh_allen_is_untouched_by_the_palmer_alias():
    """No nickname rule was added, so the Josh/Joshua pair in this very
    game stays unaffected."""

    r = _resolve("Josh Allen", _snapshot(PALMER, ALLEN))
    assert r.outcome == "RESOLVED"
    assert r.stable_id == "00-0034857"
    assert _resolve("Joshua Allen", _snapshot(PALMER, ALLEN)).outcome == "UNRESOLVED_PLAYER", (
        "there is no Josh <-> Joshua expansion; only the reviewed alias exists"
    )


def test_an_alias_cannot_pull_a_player_into_a_game_he_is_not_in():
    """The alias names an identity; that identity must still be found on
    one of THIS event's two teams."""

    r = _resolve("Joshua Palmer", _snapshot(GOFF, ALLEN))  # no Palmer on the pool
    assert r.outcome == "UNRESOLVED_PLAYER"
    assert "refusing to force it into the game" in r.detail


def test_an_alias_stops_working_when_the_player_changes_teams():
    """Intended behaviour, not a limitation: the two-team constraint is
    applied to the alias target exactly as to an exact match."""

    r = _resolve(
        "Joshua Palmer",
        _snapshot(PALMER, GOFF),
        home=CanonicalTeam.KC,
        away=CanonicalTeam.SF,
    )
    assert r.outcome == "UNRESOLVED_PLAYER"


def test_the_alias_is_scoped_to_one_provider():
    assert _resolve("Joshua Palmer", _snapshot(PALMER), provider="SOME_OTHER_FEED").outcome == (
        "UNRESOLVED_PLAYER"
    )
    assert _resolve("Joshua Palmer", _snapshot(PALMER), provider=None).outcome == (
        "UNRESOLVED_PLAYER"
    )


def test_no_general_nickname_fuzzy_or_suffix_logic_was_introduced():
    """Checks EXECUTABLE code only.

    The module's own prose says "no fuzzy matching, no nickname handling",
    so a naive substring scan over the whole source matches the very
    statement that the techniques are absent.
    """

    import ast
    import inspect

    from app.rosterdata import resolution

    tree = ast.parse(inspect.getsource(resolution))
    # Drop every docstring, then unparse back to pure code.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree).lower()

    for forbidden in ("difflib", "levenshtein", "fuzz", "nickname", "startswith", "soundex"):
        assert forbidden not in code, f"{forbidden} appeared in resolver CODE"


def test_resolver_version_records_the_alias_behaviour():
    assert RESOLVER_VERSION == "two-team-exact-alias-v2"


# --- the two ages are never conflated ----------------------------------


def test_live_ingest_reports_observation_age_separately_from_market_change_age():
    """The methodology correction.

    provider_market_updated_at is diagnostic only per the market seam §3.
    A market left unchanged for an hour and successfully re-fetched one
    second ago is a FRESH observation of a quiet market -- so vendor
    last_update age must never be presented as quote freshness.
    """

    import inspect

    from app.marketdata import live_ingest

    report = live_ingest.Report()
    assert hasattr(report, "observation_age_seconds")
    assert hasattr(report, "provider_market_change_age_seconds")
    assert not hasattr(report, "quote_age_seconds"), "the ambiguous field is gone"

    rendered = live_ingest.render(report)
    assert "OBSERVATION AGE" in rendered
    assert "PROVIDER MARKET-CHANGE AGE — DIAGNOSTIC ONLY" in rendered
    assert "MUST NOT drive checkpoint eligibility" in rendered

    source = inspect.getsource(live_ingest)
    assert "snapshot_taken_at - q.as_of_at" in source, (
        "observation age must be measured against the snapshot clock"
    )
