"""The benchmark slate size is METHODOLOGY, and it is 10.

This file exists because the Phase 4A.7 allocator comparison was run at
FIVE slots. Five is not a number any governing document specifies — it came
from test fixtures — and the conclusions did not survive being redone at
ten. `ROUND_ROBIN_BY_KICKOFF_V0` went from "biased" to "draws every slot
from two of six kickoff blocks", and `KICKOFF_BLOCK_STRATIFIED_V1`'s
block-exclusion defect stopped manifesting entirely.

A review conducted under the wrong sampling regime is not a review, so the
intended size is pinned here against the documents that define it.
"""

from __future__ import annotations

import pathlib
import re

INTENDED_BENCHMARK_SLATE_SIZE = 10
"""CONSTITUTION.md §20, RULES.md §7, ARCHITECTURE.md §4a and the Phase 4
ingestion seam doc all say 10. Changing production to any other value is a
METHODOLOGY amendment, append-only, never a test convenience."""

INTENDED_PROP_TYPE_COUNT = 5
"""RULES.md §6. Ten slots over five types is two targets per stat type."""

ROOT = pathlib.Path(__file__).resolve().parents[4]


def _doc(name: str) -> str:
    path = ROOT / name
    assert path.exists(), f"{name} not found at {path}; the guard is vacuous"
    return path.read_text()


def test_the_governing_documents_say_ten():
    rules = _doc("RULES.md")
    assert "`benchmark_slate_size = 10`" in rules, (
        "RULES.md no longer pins the slate size; a methodology change must be "
        "deliberate and documented, not discovered later in a dry run"
    )
    architecture = _doc("ARCHITECTURE.md")
    assert re.search(r"benchmark_slate_size.{0,4}\(10\)", architecture), (
        "ARCHITECTURE.md no longer states the slot count the allocator runs at"
    )
    constitution = _doc("CONSTITUTION.md")
    assert "10 props" in constitution, "CONSTITUTION.md §20 no longer says 10 props"


def test_the_production_provisioner_creates_the_intended_size():
    """The durable research-season provisioner is what actually writes a
    production SeasonRules row. If it drifts from the documents, every
    season created afterwards runs a methodology nobody approved."""

    import inspect

    from app.db.repositories.season_repository import SeasonRepository

    source = inspect.getsource(SeasonRepository.create_season_rules)
    match = re.search(r"benchmark_slate_size\s*=\s*(\d+)", source)
    assert match, "the provisioner no longer sets benchmark_slate_size at all"
    assert int(match.group(1)) == INTENDED_BENCHMARK_SLATE_SIZE, (
        f"the provisioner creates benchmark_slate_size={match.group(1)}, but the "
        f"governing documents say {INTENDED_BENCHMARK_SLATE_SIZE}. If the "
        "methodology genuinely changed, amend the documents and the season "
        "rules append-only -- do not let the provisioner drift."
    )


def test_ten_slots_over_five_prop_types_is_two_targets_each():
    """The shape the slate core produces at the production size. Two per
    stat type is a property of 10/5, so a change to either number silently
    changes the research design."""

    from app.db.repositories.season_repository import SeasonRepository
    import inspect

    source = inspect.getsource(SeasonRepository.create_season_rules)
    types = re.search(r"supported_prop_types=\[(.*?)\]", source, re.S)
    assert types, "the provisioner no longer sets supported_prop_types"
    count = len(re.findall(r'"', types.group(1))) // 2
    assert count == INTENDED_PROP_TYPE_COUNT, (
        f"the provisioner supplies {count} prop types, not "
        f"{INTENDED_PROP_TYPE_COUNT}; the two-targets-per-type property of a "
        "ten-slot slate no longer holds"
    )
    assert INTENDED_BENCHMARK_SLATE_SIZE % INTENDED_PROP_TYPE_COUNT == 0


def test_the_allocator_module_does_not_claim_a_five_slot_world():
    """Documentation that encodes the wrong regime is how the wrong regime
    gets reviewed."""

    import app.forecast_lab.slate_allocation as module

    assert module.__doc__
    assert "How five benchmark slots" not in module.__doc__, (
        "the allocator module still describes a five-slot world"
    )
    assert "10" in module.__doc__, (
        "the allocator module should name the slot count it is reviewed at"
    )


def test_the_rejection_evidence_is_stated_at_the_production_slot_count():
    """A defect measured at five slots is not evidence about a ten-slot
    season. V0's rejection in particular is far more severe at ten -- all
    ten slots from two kickoff blocks -- and the recorded reason has to say
    so, or the next reviewer re-derives it from the wrong regime."""

    from app.forecast_lab.slate_allocation import ALLOCATION_METHODS

    v0 = ALLOCATION_METHODS["ROUND_ROBIN_BY_KICKOFF_V0"]
    assert not v0.approved
    assert "all ten" in v0.defect, (
        "V0's recorded defect no longer states the production-slot-count "
        f"evidence: {v0.defect}"
    )
    assert "2 of 6" in v0.defect and "3 of 7" in v0.defect, (
        "V0's defect no longer cites the real 2026 Week-3 and Week-4 coverage"
    )

    block = ALLOCATION_METHODS["KICKOFF_BLOCK_STRATIFIED_V1"]
    assert not block.approved
    assert "does NOT occur" in block.defect, (
        "the block candidate's defect must record that its five-slot "
        "exclusion does not manifest at ten slots, rather than carrying the "
        "old verdict forward unexamined"
    )
    assert "6/6" in block.defect and "7/7" in block.defect

    stratified = ALLOCATION_METHODS["STRATIFIED_BY_KICKOFF_V1"]
    assert not stratified.approved
    assert "10 slots" in stratified.defect
