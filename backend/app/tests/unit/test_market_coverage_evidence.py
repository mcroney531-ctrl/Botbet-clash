"""What the competitors are told about market-evidence quality (4A.4 §4).

The decision this locks in: expose the DEGRADATION, never the rejected
prices. A competitor learns that two books were dropped as stale; it does
not learn what those books were quoting, and it never sees the vendor's
`last_update` — a field the freshness rule itself is forbidden to use.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.ai.prompts.benchmark_forecasting import SYSTEM_INSTRUCTIONS
from app.ai.prompts.versions import BENCHMARK_PROMPT_VERSION, FORECAST_SCHEMA_VERSION
from app.ai.schemas.benchmark_forecast import MarketContext
from app.forecast_lab.evidence_service import market_context_payload, mock_evidence_payload

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]


@dataclass
class FakeSnapshot:
    canonical_line: Decimal | None = Decimal("74.5")
    canonical_over_price: int | None = -165
    canonical_under_price: int | None = 129
    market_median_line: Decimal | None = Decimal("74.5")
    number_of_books: int = 3
    books_observed: int = 5
    stale_books_excluded: int = 2
    canonical_quote_stale: bool = False


def test_degraded_coverage_is_distinguishable_from_thin_coverage():
    """The ambiguity this whole section exists to remove: `number_of_books:
    3` must not read identically whether three books existed or five
    existed and two were refused."""

    degraded = market_context_payload(FakeSnapshot())
    thin = market_context_payload(
        FakeSnapshot(number_of_books=3, books_observed=3, stale_books_excluded=0)
    )

    assert degraded["number_of_books"] == thin["number_of_books"] == 3
    assert degraded["books_observed"] == 5 and thin["books_observed"] == 3
    assert degraded["stale_books_excluded"] == 2 and thin["stale_books_excluded"] == 0


def test_the_payload_never_carries_rejected_prices_or_the_vendor_clock():
    """Competitors get to know the evidence was degraded. They do not get
    the rejected prices back through a side door, and they never get the
    field the freshness rule is itself forbidden to consult."""

    payload = mock_evidence_payload(
        generated_at=__import__("datetime").datetime(2026, 9, 17, tzinfo=__import__("datetime").timezone.utc),
        market_snapshot=FakeSnapshot(),
    )
    rendered = repr(payload)
    for forbidden in ("provider_market_updated_at", "last_update", "excluded_stale", "selected_quotes"):
        assert forbidden not in rendered

    context = payload["market_context"]
    assert set(context) == {
        "market_median_line",
        "number_of_books",
        "books_observed",
        "stale_books_excluded",
        "canonical_quote_stale",
    }


def test_the_canonical_stale_flag_reaches_the_competitor():
    """A stale canonical book means no baseline and no substitute. A
    forecaster reading a null canonical market should be able to tell that
    from a market that was never priced."""

    payload = market_context_payload(FakeSnapshot(canonical_quote_stale=True))
    assert payload["canonical_quote_stale"] is True


def test_the_request_schema_rejects_coverage_that_does_not_add_up():
    """A book falling out of both counts is how a dropped feed turns into a
    snapshot that merely looks thin — caught at the schema boundary, not
    only by a database CHECK the request never touches."""

    with pytest.raises(ValueError, match="books_observed"):
        MarketContext(
            median_line=Decimal("74.5"),
            min_line=Decimal("73.5"),
            max_line=Decimal("75.5"),
            books=3,
            books_observed=5,
            stale_books_excluded=0,
        )


def test_consistent_coverage_is_accepted():
    ctx = MarketContext(
        median_line=Decimal("74.5"),
        min_line=Decimal("73.5"),
        max_line=Decimal("75.5"),
        books=3,
        books_observed=5,
        stale_books_excluded=2,
    )
    assert ctx.books_observed == ctx.books + ctx.stale_books_excluded


def test_the_new_fields_are_explained_in_the_instruction_not_just_named():
    """The v1 -> v2 lesson, applied. Bare integers carry no semantics: three
    providers all independently misread `confidence` when its meaning lived
    only in a schema keyword. `books_observed` would be read the same way."""

    for term in ("books_observed", "stale_books_excluded", "canonical_quote_stale"):
        assert term in SYSTEM_INSTRUCTIONS
    assert "evidence quality" in SYSTEM_INSTRUCTIONS
    assert "no other book is substituted" in SYSTEM_INSTRUCTIONS


def test_the_prompt_version_was_bumped_and_the_response_schema_was_not():
    """Changing what every competitor is shown is a prompt change. The
    response schema is untouched, so its version must not move — bumping it
    would falsely invalidate every stored forecast's shape contract."""

    assert BENCHMARK_PROMPT_VERSION == "benchmark-v3"
    assert FORECAST_SCHEMA_VERSION == "forecast-v1"


def test_the_renderer_refuses_a_stale_prompt_version():
    """A season already running under benchmark-v2 must not silently
    acquire v3's fields mid-season. `prompt_version` is persisted on every
    AgentSession and is the reproducibility contract."""

    from app.ai.prompts.benchmark_forecasting import render_benchmark_prompt
    from app.ai.schemas.benchmark_forecast import BenchmarkForecastRequest, MarketInput

    request = BenchmarkForecastRequest(
        prompt_version="benchmark-v2",
        schema_version=FORECAST_SCHEMA_VERSION,
        season=2026,
        week=3,
        checkpoint="FINAL",
        markets=[
            MarketInput(
                market_id="m1",
                player="Player X",
                team="BUF",
                opponent="DET",
                stat_type="receiving_yards",
                canonical_line=Decimal("74.5"),
                canonical_over_price=-165,
                canonical_under_price=129,
                canonical_market_probability_over=Decimal("0.58777"),
                market_context=MarketContext(
                    median_line=Decimal("74.5"),
                    min_line=Decimal("74.5"),
                    max_line=Decimal("74.5"),
                    books=1,
                    books_observed=1,
                    stale_books_excluded=0,
                ),
                evidence={},
            )
        ],
    )
    with pytest.raises(ValueError, match="benchmark-v3"):
        render_benchmark_prompt(request)


def test_the_instruction_stays_competitor_neutral():
    """Every competitor sees the same coverage metadata. Shared evidence
    stops being shared the moment one of them is told something else.

    Asserted against the TEXT COMPETITORS RECEIVE, not against the module:
    the module's history comments legitimately name the providers whose
    behaviour forced v2, and matching those would be matching prose rather
    than the prompt.
    """

    lowered = SYSTEM_INSTRUCTIONS.lower()
    for vendor in ("openai", "anthropic", "gemini", "google", "claude", "gpt"):
        assert vendor not in lowered, f"{vendor} named in the shared instruction"


def test_the_evidence_payload_helper_is_the_only_place_that_shapes_it():
    """One builder, so the frozen EvidenceSnapshot and the live request
    cannot drift into showing different coverage for the same snapshot."""

    from app.forecast_lab import evidence_service

    tree = ast.parse(inspect.getsource(evidence_service))
    builders = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and "market_context" in n.name
    ]
    assert builders == ["market_context_payload"]
