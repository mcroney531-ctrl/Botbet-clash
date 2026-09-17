"""Pure unit tests for the market-data seam. No database, no network."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import StatFamily
from app.marketdata.dto import (
    ProviderEvent,
    ProviderEventRef,
    ProviderPlayerRef,
    ProviderQuote,
    normalize_player_name,
)
from app.marketdata.ingestion import (
    canonical_line,
    partition_ambiguous_lines,
    quote_fingerprint,
)
from app.marketdata.mapping import (
    UnverifiedMarketMappingError,
    resolve_stat_family,
    vendor_keys_for,
)
from app.marketdata.providers.mock import MockMarketDataProvider
from app.marketdata.telemetry import sanitize_message

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
SENTINEL_KEY = "SENTINEL_ODDS_KEY_d34db33f"


def _ref() -> ProviderEventRef:
    return ProviderEventRef(provider="T", external_event_id="evt-1")


def _player(**kw) -> ProviderPlayerRef:
    return ProviderPlayerRef(provider="T", display_name=kw.pop("name", "A Player"), **kw)


def _quote(**kw) -> ProviderQuote:
    defaults = dict(
        event=_ref(),
        player=_player(),
        stat_family=StatFamily.RECEIVING_YARDS,
        vendor_market_key="player_reception_yds",
        sportsbook="DRAFTKINGS",
        line=Decimal("74.5"),
        over_price=-115,
        under_price=-105,
        as_of_at=NOW,
        retrieved_at=NOW,
        source="T",
    )
    defaults.update(kw)
    return ProviderQuote(**defaults)


# --- 1. DTO timezone validation ---------------------------------------


def test_naive_datetimes_are_rejected_at_the_dto_boundary():
    with pytest.raises(ValueError, match="timezone-aware"):
        _quote(as_of_at=datetime(2026, 9, 16, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        ProviderEvent(
            ref=_ref(),
            sport_key="americanfootball_nfl",
            kickoff_at=datetime(2026, 9, 16, 12, 0),
            home_team="KC",
            away_team="SF",
        )


def test_as_of_after_retrieved_is_rejected():
    # A provider claiming a snapshot from the future is malformed, not a quote.
    with pytest.raises(ValueError, match="cannot be observed before it existed"):
        _quote(as_of_at=NOW + timedelta(hours=1), retrieved_at=NOW)


def test_float_lines_and_bool_prices_are_rejected():
    with pytest.raises(TypeError, match="Decimal"):
        _quote(line=74.5)
    with pytest.raises(TypeError, match="bool"):
        _quote(over_price=True)


# --- 2. Decimal normalization / fingerprint stability ------------------


def test_decimal_formatting_does_not_change_the_fingerprint():
    assert canonical_line(Decimal("74.5")) == canonical_line(Decimal("74.50"))

    call_id = uuid.uuid4()
    base = dict(
        provider_call_id=call_id,
        external_event_id="evt-1",
        player_external_ref="T:id:p1",
        stat_family="receiving_yards",
        sportsbook="DRAFTKINGS",
        over_price=-115,
        under_price=-105,
        parser_version="p1",
    )
    # Decimal("74.50") is what a Numeric(6,2) round-trip hands back.
    assert quote_fingerprint(line=Decimal("74.5"), **base) == quote_fingerprint(
        line=Decimal("74.50"), **base
    )


def test_fingerprint_field_boundaries_cannot_be_impersonated():
    call_id = uuid.uuid4()
    base = dict(
        provider_call_id=call_id,
        stat_family="receiving_yards",
        sportsbook="DRAFTKINGS",
        line=Decimal("74.5"),
        over_price=-115,
        under_price=-105,
        parser_version="p1",
    )
    a = quote_fingerprint(external_event_id="evt", player_external_ref="1:x", **base)
    b = quote_fingerprint(external_event_id="evt1", player_external_ref=":x", **base)
    assert a != b


# --- 3/4/5. Replay idempotency, new observation, parser version --------


def test_same_call_same_quote_is_the_same_fingerprint():
    call_id = uuid.uuid4()
    args = dict(
        provider_call_id=call_id,
        external_event_id="evt-1",
        player_external_ref="T:id:p1",
        stat_family="receiving_yards",
        sportsbook="DRAFTKINGS",
        line=Decimal("74.5"),
        over_price=-115,
        under_price=-105,
        parser_version="p1",
    )
    assert quote_fingerprint(**args) == quote_fingerprint(**args)


def test_a_new_call_with_identical_odds_is_a_different_observation():
    args = dict(
        external_event_id="evt-1",
        player_external_ref="T:id:p1",
        stat_family="receiving_yards",
        sportsbook="DRAFTKINGS",
        line=Decimal("74.5"),
        over_price=-115,
        under_price=-105,
        parser_version="p1",
    )
    first = quote_fingerprint(provider_call_id=uuid.uuid4(), **args)
    second = quote_fingerprint(provider_call_id=uuid.uuid4(), **args)
    assert first != second, (
        "a fresh poll must record a new observation even when nothing moved — "
        "that is what distinguishes 'still quoted' from 'book disappeared'"
    )


def test_parser_version_changes_the_fingerprint():
    args = dict(
        provider_call_id=uuid.uuid4(),
        external_event_id="evt-1",
        player_external_ref="T:id:p1",
        stat_family="receiving_yards",
        sportsbook="DRAFTKINGS",
        line=Decimal("74.5"),
        over_price=-115,
        under_price=-105,
    )
    assert quote_fingerprint(parser_version="p1", **args) != quote_fingerprint(
        parser_version="p2", **args
    )


# --- 6. No global parser-version visibility filter ---------------------


def test_quote_selection_does_not_filter_on_parser_version():
    """A parser upgrade must not be able to erase history.

    If quotes_as_of ever gained a parser_version predicate, deploying v2
    would make every week still parsed by v1 vanish from checkpoints until
    each archived response had been replayed. The supersession policy is
    deliberately deferred; until it exists, parser_version is provenance.
    """

    import inspect

    from app.db.repositories.market_repository import MarketRepository

    source = inspect.getsource(MarketRepository.quotes_as_of)
    assert "parser_version" not in source


# --- 11/13/14. Mapping and alternate lines ----------------------------


def test_unknown_vendor_key_maps_to_nothing_rather_than_something_close():
    # player_pass_attempts is adjacent to passing_yards. It must not resolve.
    assert resolve_stat_family("player_pass_attempts", verified_only=False) is None
    assert resolve_stat_family("player_pass_yds", verified_only=False) is StatFamily.PASSING_YARDS


def test_all_five_market_keys_are_verified_by_a_real_payload():
    """Promoted 2026-09-17 after the live probe returned all five verbatim.

    Documentation was never enough to populate VERIFIED_MARKET_KEYS --
    observation was. The gate itself still works: a family with no
    verified spelling raises rather than silently requesting fewer.
    """

    assert vendor_keys_for(list(StatFamily), verified_only=True) == (
        "player_pass_yds",
        "player_pass_tds",
        "player_rush_yds",
        "player_receptions",
        "player_reception_yds",
    )


def test_the_verified_gate_still_refuses_a_family_with_no_verified_key(monkeypatch):
    """Promotion filled the table; it did not remove the gate.

    If a sixth family were added, or a verified spelling withdrawn,
    production must refuse rather than silently request fewer families --
    a short request would look like "DraftKings doesn't offer that" in the
    resulting data.
    """

    from types import MappingProxyType

    from app.marketdata import mapping

    partial = {
        k: v
        for k, v in mapping.VERIFIED_MARKET_KEYS.items()
        if v is not StatFamily.RECEIVING_YARDS
    }
    monkeypatch.setattr(mapping, "VERIFIED_MARKET_KEYS", MappingProxyType(partial))
    with pytest.raises(UnverifiedMarketMappingError, match="validation probe"):
        mapping.vendor_keys_for([StatFamily.RECEIVING_YARDS], verified_only=True)


def test_ambiguous_alternate_lines_quarantine_the_whole_set():
    dk_main = _quote(line=Decimal("74.5"))
    dk_alt = _quote(line=Decimal("79.5"), over_price=130, under_price=-160)
    fd = _quote(sportsbook="FANDUEL", line=Decimal("74.5"), over_price=-110, under_price=-110)

    accepted, diagnostics = partition_ambiguous_lines([dk_main, dk_alt, fd])

    accepted_books = {q.sportsbook for q in accepted}
    assert accepted_books == {"FANDUEL"}, "neither DK candidate may survive"
    assert [d.category for d in diagnostics] == ["AMBIGUOUS_ALTERNATE_LINE"]
    assert diagnostics[0].sportsbook == "DRAFTKINGS"


def test_no_arbitrary_first_line_behaviour_regardless_of_input_order():
    dk_main = _quote(line=Decimal("74.5"))
    dk_alt = _quote(line=Decimal("79.5"), over_price=130, under_price=-160)
    forward, _ = partition_ambiguous_lines([dk_main, dk_alt])
    reverse, _ = partition_ambiguous_lines([dk_alt, dk_main])
    assert forward == [] and reverse == [], (
        "ordering must never decide the canonical line: both orders refuse both"
    )


def test_the_same_line_repeated_in_one_call_is_not_an_alternate():
    duplicate = _quote()
    accepted, diagnostics = partition_ambiguous_lines([duplicate, duplicate])
    assert diagnostics == []
    assert len(accepted) == 2  # the fingerprint collapses these on insert


# --- 18. Quota header parsing -----------------------------------------


def test_quota_headers_are_parsed_from_the_provider_response():
    import httpx

    from app.marketdata.providers.the_odds_api import TheOddsApiProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={
                "x-requests-used": "137",
                "x-requests-remaining": "9863",
                "x-requests-last": "5",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TheOddsApiProvider(api_key=SENTINEL_KEY, client=client)
    result = provider.list_events(window_start=NOW, window_end=NOW + timedelta(days=7))

    assert result.ok
    assert result.call_metadata.quota_used == 137
    assert result.call_metadata.quota_remaining == 9863
    assert result.call_metadata.quota_cost == 5
    assert result.call_metadata.raw_response_sha256 is not None
    assert result.call_metadata.raw_response_bytes == len(b"[]")


# --- 21. The sentinel key must never surface anywhere ------------------


@pytest.mark.parametrize(
    "raiser",
    [
        pytest.param(lambda r: httpx_connect_error(r), id="transport"),
        pytest.param(lambda r: httpx_timeout(r), id="timeout"),
    ],
)
def test_transport_failures_never_leak_the_credential(raiser):
    """httpx puts the full request URL in its exception text, and this
    provider authenticates by query parameter — so str(exc) is a leak."""

    import httpx

    from app.marketdata.providers.the_odds_api import TheOddsApiProvider

    client = httpx.Client(transport=httpx.MockTransport(raiser))
    provider = TheOddsApiProvider(api_key=SENTINEL_KEY, client=client)
    result = provider.list_events(window_start=NOW, window_end=NOW + timedelta(days=7))

    assert result.error is not None
    haystack = repr(result)
    assert SENTINEL_KEY not in haystack
    assert "apiKey" not in result.error.message


def httpx_connect_error(request):
    import httpx

    raise httpx.ConnectError("failed", request=request)


def httpx_timeout(request):
    import httpx

    raise httpx.ReadTimeout("slow", request=request)


def test_sanitizer_strips_urls_and_credential_assignments():
    leaky = (
        f"ConnectError for https://api.the-odds-api.com/v4/x?apiKey={SENTINEL_KEY}&regions=us"
    )
    cleaned = sanitize_message(leaky)
    assert SENTINEL_KEY not in cleaned
    assert sanitize_message(f"apiKey={SENTINEL_KEY}") == "[redacted-credential]"


def test_missing_credential_message_names_the_variable_not_a_value(monkeypatch):
    from app.marketdata.providers.the_odds_api import TheOddsApiProvider

    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    provider = TheOddsApiProvider()
    result = provider.list_events(window_start=NOW, window_end=NOW + timedelta(days=7))
    assert result.error.category == "AUTHENTICATION_ERROR"
    assert "THE_ODDS_API_KEY" in result.error.message
    assert "Railway" in result.error.message


# --- Player identity guards -------------------------------------------


def test_suffixes_survive_name_normalization():
    assert normalize_player_name("Odell Beckham Jr.") != normalize_player_name("Odell Beckham")
    assert normalize_player_name("  Ken   WALKER  III ") == "ken walker iii"


def test_player_external_ref_records_which_identity_scheme_was_used():
    with_id = _player(external_player_id="p1").as_external_ref()
    without = _player().as_external_ref()
    assert with_id == "T:id:p1"
    assert without.startswith("T:name:")


# --- 19. Unexpected adapter exceptions are normalized ------------------


def test_an_unexpected_client_error_becomes_a_normalized_failure():
    import httpx

    from app.marketdata.providers.the_odds_api import TheOddsApiProvider

    def explode(request):
        raise RuntimeError(f"surprise containing {SENTINEL_KEY}")

    client = httpx.Client(transport=httpx.MockTransport(explode))
    provider = TheOddsApiProvider(api_key=SENTINEL_KEY, client=client)
    result = provider.list_events(window_start=NOW, window_end=NOW + timedelta(days=7))

    assert result.error.category == "UNKNOWN_PROVIDER_ERROR"
    assert SENTINEL_KEY not in repr(result)


# --- Mock provider covers the failure surface -------------------------


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("AUTH_FAILURE", "AUTHENTICATION_ERROR"),
        ("QUOTA_EXHAUSTED", "QUOTA_EXHAUSTED"),
        ("RATE_LIMITED", "RATE_LIMITED"),
        ("TIMEOUT", "TIMEOUT"),
        ("TRANSPORT_FAILURE", "PROVIDER_UNAVAILABLE"),
        ("MALFORMED", "MALFORMED_RESPONSE"),
    ],
)
def test_mock_provider_reproduces_every_transport_failure(scenario, expected):
    provider = MockMarketDataProvider(scenario=scenario, now=NOW)
    result = provider.fetch_quotes(event=_ref(), stat_families=[StatFamily.RECEIVING_YARDS])
    assert result.error.category == expected
    assert result.payload is None


def test_historical_mock_reports_provider_snapshot_time_not_our_clock():
    snapshot = NOW - timedelta(days=11)
    provider = MockMarketDataProvider(scenario="STANDARD", now=NOW)
    result = provider.fetch_quotes_as_of(
        event=_ref(), stat_families=[StatFamily.RECEIVING_YARDS], as_of=snapshot
    )
    assert result.ok
    for quote in result.payload:
        assert quote.as_of_at == snapshot, "historical as_of_at must be the provider's snapshot"
        assert quote.retrieved_at == NOW, "retrieved_at stays our clock"
    assert result.call_metadata.provider_snapshot_at == snapshot


def test_probe_report_never_claims_pass():
    from app.marketdata.validation_probe import ProbeFindings, render_report

    report = render_report(ProbeFindings())
    assert "COMPLETE — REVIEW REQUIRED" in report
    assert "PHASE 4: PASS" not in report


# --- Shape discovery: vendor parsing stays inside the adapter ----------


def _odds_payload(*, outcomes, market_key="player_reception_yds", book="draftkings",
                  market_last_update="2026-09-16T11:52:00Z",
                  book_last_update="2026-09-16T11:50:00Z"):
    market = {"key": market_key, "outcomes": outcomes}
    if market_last_update is not None:
        market["last_update"] = market_last_update
    bookmaker = {"key": book, "markets": [market]}
    if book_last_update is not None:
        bookmaker["last_update"] = book_last_update
    return {"id": "evt-1", "bookmakers": [bookmaker]}


def _discover(payload, *, families=(StatFamily.RECEIVING_YARDS,)):
    import httpx

    from app.marketdata.providers.the_odds_api import TheOddsApiProvider

    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    provider = TheOddsApiProvider(api_key=SENTINEL_KEY, client=client)
    return provider.discover_event_shape(
        event=ProviderEventRef(provider="THE_ODDS_API", external_event_id="evt-1"),
        stat_families=list(families),
        now=NOW,
    )


def test_deliverable_nine_is_a_matched_over_under_pair_not_one_outcome():
    """The contract is a normalized ProviderQuote, which needs BOTH sides."""

    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105},
            ]
        )
    )
    quote = shape.sample_quote
    assert quote is not None, shape.sample_quote_unavailable_reason
    assert isinstance(quote, ProviderQuote)
    assert quote.line == Decimal("74.5")
    assert quote.over_price == -115 and quote.under_price == -105
    assert quote.stat_family is StatFamily.RECEIVING_YARDS
    assert quote.sportsbook == "DRAFTKINGS"
    # market-level last_update is what we bind, not the bookmaker-level one
    assert quote.provider_market_updated_at.isoformat() == "2026-09-16T11:52:00+00:00"


def test_an_unpaired_outcome_is_reported_as_unavailable_not_as_a_quote():
    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115},
            ]
        )
    )
    assert shape.sample_quote is None
    assert "unpaired" in (shape.sample_quote_unavailable_reason or "")
    assert any(d.category == "INCOMPLETE_PRICE_PAIR" for d in shape.diagnostics)


def test_a_generic_id_is_not_treated_as_a_stable_player_identifier():
    """A bare `id` could key an outcome rather than a person; guessing wrong
    silently merges or splits real people in Player.external_ref."""

    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115,
                 "id": "abc123"},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105,
                 "id": "def456"},
            ]
        )
    )
    assert shape.player_identity_verified is False
    assert "UNCLASSIFIED_IDENTIFIER" in shape.player_identity
    assert "STABLE_PLAYER_ID" not in shape.player_identity
    # ...and it must not become part of the persisted identity.
    assert shape.sample_quote.player.external_player_id is None
    assert shape.sample_quote.player.as_external_ref().startswith("THE_ODDS_API:name:")


def test_an_explicitly_player_semantic_id_does_qualify():
    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115,
                 "player_id": "p-9"},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105,
                 "player_id": "p-9"},
            ]
        )
    )
    assert shape.player_identity_verified is True
    assert "STABLE_PLAYER_ID (field=player_id)" == shape.player_identity
    assert shape.sample_quote.player.as_external_ref() == "THE_ODDS_API:id:p-9"


def test_both_last_update_levels_are_reported_when_the_vendor_sends_both():
    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105},
            ]
        )
    )
    assert set(shape.last_update_levels) == {"BOOKMAKER", "MARKET"}
    assert shape.market_last_update_sample == "2026-09-16T11:52:00Z"


def test_multiple_lines_in_one_market_key_are_flagged():
    _, shape = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105},
                {"name": "Over", "description": "A Player", "point": 79.5, "price": 130},
                {"name": "Under", "description": "A Player", "point": 79.5, "price": -160},
            ]
        )
    )
    assert shape.alternate_line_shape.startswith("MULTIPLE_LINES_WITHIN_ONE_MARKET_KEY")


def test_the_probe_module_does_not_reference_vendor_json_field_names():
    """The seam says vendor field names live only in the provider adapter.
    Diagnostic code is not exempt — that exemption is how schema leakage
    starts."""

    import inspect

    from app.marketdata import validation_probe

    source = inspect.getsource(validation_probe)
    # Strip docstrings/comments: the module may DISCUSS these names in prose.
    code_only = "\n".join(
        line.split("#")[0] for line in source.splitlines() if not line.strip().startswith("#")
    )
    for vendor_field in ('"bookmakers"', '"outcomes"', '"markets"', '"last_update"', '"point"'):
        assert vendor_field not in code_only, f"{vendor_field} leaked above the provider boundary"


def test_every_call_reports_telemetry_not_just_the_last():
    results, _ = _discover(
        _odds_payload(
            outcomes=[
                {"name": "Over", "description": "A Player", "point": 74.5, "price": -115},
                {"name": "Under", "description": "A Player", "point": 74.5, "price": -105},
            ]
        ),
        families=tuple(StatFamily),
    )
    assert len(results) == 5, "one call per family, each with its own quota telemetry"
    assert all(r.call_metadata.raw_response_sha256 for r in results)
