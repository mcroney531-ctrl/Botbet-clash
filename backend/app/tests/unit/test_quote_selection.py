"""The selection rule as a pure function, plus the guards that keep it the
ONLY implementation of itself.

These need no database. That is the point of extracting the rule: a
calibration preview that re-implements it proves nothing about the rule
production uses, so there has to be exactly one copy and it has to be
exercisable without writing anything.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.forecast_lab.quote_selection import (
    FreshnessConfigError,
    QuoteObservation,
    plan_selection,
    validate_max_observation_age,
)

NOW = datetime(2026, 9, 17, 20, 10, 56, 225032, tzinfo=timezone.utc)
# backend/ -- app/tests/unit/<this file> is three levels down.
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]


def obs(sportsbook: str, *, age_seconds: float, line="74.5", over=-165, under=129, retrieved_offset=0.0):
    as_of = NOW - timedelta(seconds=age_seconds)
    return QuoteObservation(
        quote_id=uuid.uuid4(),
        sportsbook=sportsbook,
        line=Decimal(line),
        over_price=over,
        under_price=under,
        as_of_at=as_of,
        retrieved_at=as_of + timedelta(seconds=retrieved_offset),
    )


# --- config validation ------------------------------------------------


def test_none_disables_the_gate():
    assert validate_max_observation_age(None) is None


@pytest.mark.parametrize("value", [0, 1, 900, 86_400])
def test_accepts_non_negative_integers(value):
    assert validate_max_observation_age(value) == value


@pytest.mark.parametrize("value", [-1, -900])
def test_rejects_a_negative_tolerance(value):
    """Not a strict rule -- a configuration error. Every observation would
    be stale, so every snapshot would lose its canonical baseline and the
    run would report a total market outage that never happened."""

    with pytest.raises(FreshnessConfigError, match=">= 0"):
        validate_max_observation_age(value)


@pytest.mark.parametrize("value", [True, False, 1.5, "900", Decimal("900")])
def test_rejects_non_integers_including_bools(value):
    """`True` is an `int` in Python and would silently become a one-second
    tolerance, which is the most destructive possible typo here."""

    with pytest.raises(FreshnessConfigError):
        validate_max_observation_age(value)


def test_the_service_validates_at_construction_not_at_first_snapshot():
    """A misconfigured tolerance must fail before a run starts, not partway
    through a slate with some snapshots already written."""

    from app.forecast_lab.market_snapshot_service import MarketSnapshotService

    with pytest.raises(FreshnessConfigError):
        MarketSnapshotService(session=None, max_observation_age_seconds=-1)


# --- the rule ---------------------------------------------------------


def test_newest_observation_per_book_wins():
    old = obs("DRAFTKINGS", age_seconds=3600, line="70.5")
    new = obs("DRAFTKINGS", age_seconds=10, line="74.5")
    plan = plan_selection(
        [old, new], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    assert plan.books_observed == 1
    assert plan.canonical.observation.line == Decimal("74.5")


def test_caller_ordering_is_not_trusted():
    """The repository's ORDER BY already produces the right order, but the
    calibration preview may assemble observations from anywhere. "Whichever
    row came first" silently deciding the canonical baseline is exactly the
    non-reproducibility the ordering exists to prevent."""

    old = obs("DRAFTKINGS", age_seconds=3600, line="70.5")
    new = obs("DRAFTKINGS", age_seconds=10, line="74.5")
    forward = plan_selection(
        [old, new], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    backward = plan_selection(
        [new, old], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    assert forward.canonical.observation.quote_id == backward.canonical.observation.quote_id


def test_ties_break_on_retrieved_at_then_id():
    """Two observations of the same market state at the same instant still
    need a deterministic winner, or the canonical baseline -- and every
    de-vigged probability built on it -- stops being reproducible."""

    first = obs("DRAFTKINGS", age_seconds=10, line="70.5", retrieved_offset=0.0)
    second = obs("DRAFTKINGS", age_seconds=10, line="74.5", retrieved_offset=5.0)
    a = plan_selection(
        [first, second], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    b = plan_selection(
        [second, first], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    assert a.canonical.observation.quote_id == b.canonical.observation.quote_id
    assert a.canonical.observation.line == Decimal("74.5"), "later retrieval wins the tie"


def test_observations_later_than_taken_at_are_not_visible():
    """A caller asking what a snapshot at `taken_at` saw cannot be handed
    things that had not happened yet."""

    future = obs("DRAFTKINGS", age_seconds=-60)
    plan = plan_selection(
        [future], taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=None
    )
    assert plan.books_observed == 0


def test_every_book_lands_in_exactly_one_bucket():
    plan = plan_selection(
        [obs("DRAFTKINGS", age_seconds=5), obs("FANDUEL", age_seconds=5000), obs("CAESARS", age_seconds=1)],
        taken_at=NOW,
        canonical_sportsbook="DRAFTKINGS",
        max_observation_age_seconds=900,
    )
    assert plan.books_observed == 3
    assert len(plan.included) + len(plan.excluded_stale) == 3
    assert plan.stale_books_excluded == 1


def test_canonical_stale_versus_canonical_absent():
    stale = plan_selection(
        [obs("DRAFTKINGS", age_seconds=5000)],
        taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=900,
    )
    absent = plan_selection(
        [obs("FANDUEL", age_seconds=5)],
        taken_at=NOW, canonical_sportsbook="DRAFTKINGS", max_observation_age_seconds=900,
    )
    assert stale.canonical_quote_stale is True and stale.canonical is None
    assert absent.canonical_quote_stale is False and absent.canonical is None


def test_the_selection_record_is_ordered_and_complete():
    plan = plan_selection(
        [obs("FANDUEL", age_seconds=5), obs("CAESARS", age_seconds=5), obs("BETMGM", age_seconds=9000)],
        taken_at=NOW,
        canonical_sportsbook="DRAFTKINGS",
        max_observation_age_seconds=900,
    )
    record = plan.as_record()
    assert record["schema"] == "market_snapshot_selection_v1"
    assert [e["sportsbook"] for e in record["included"]] == ["CAESARS", "FANDUEL"]
    assert [e["sportsbook"] for e in record["excluded_stale"]] == ["BETMGM"]
    assert all("observation_age_seconds" in e for e in record["included"])


# --- structural guards ------------------------------------------------


def _module_sources() -> dict[pathlib.Path, ast.Module]:
    out: dict[pathlib.Path, ast.Module] = {}
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        out[path] = ast.parse(path.read_text())
    # A structural guard that scans an empty set passes vacuously, which is
    # worse than no guard: it reports "no offenders" forever. This caught a
    # wrong APP_ROOT on the first run of this very file.
    assert len(out) > 40, f"module scan found only {len(out)} files; the root is wrong"
    return out


def test_only_quote_selection_compares_an_observation_age_to_a_tolerance():
    """There must be exactly ONE implementation of staleness.

    A second copy -- in a preview, a report, a CLI -- is the failure mode
    this refactor exists to prevent: it can drift from the rule production
    uses while still looking like evidence about it.
    """

    offenders = []
    for path, tree in _module_sources().items():
        if path.name == "quote_selection.py":
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            # ORDERING comparisons only. `x is None` / `x is not None` on a
            # tolerance is a presence check -- reports and renderers legitimately
            # ask whether a gate was configured. What must exist in exactly one
            # place is the ordering test that DECIDES staleness.
            if not any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops):
                continue
            rendered = ast.unparse(node)
            if "max_observation_age" in rendered or "max_age" in rendered:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {rendered}")
    assert offenders == [], "staleness compared outside quote_selection.py:\n" + "\n".join(offenders)


def test_the_rule_cannot_see_the_vendor_market_change_time():
    """`provider_market_updated_at` is not a field on QuoteObservation, so
    the rule cannot consult it even by accident. Asserted rather than
    assumed: this is the single methodology error the phase exists to
    prevent, and a field added "for diagnostics" would quietly make it
    reachable again."""

    fields = set(QuoteObservation.__dataclass_fields__)
    assert "provider_market_updated_at" not in fields
    source = (BACKEND_ROOT / "app" / "forecast_lab" / "quote_selection.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "provider_market_updated_at"
