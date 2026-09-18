"""How a week's benchmark slots are chosen from its fixture pool. Pure.

The slot count comes from `SeasonRules.benchmark_slate_size`, which the
constitution (§20), RULES.md §7 and ARCHITECTURE.md all put at **10**. The
first version of this comparison was run at five, which is not a number any
document specifies -- it came from test fixtures -- and the conclusions did
not survive being redone at ten. Both are recorded below because the
difference is the point.

**Nothing here is frozen.** The V1 candidates exist to be compared on
methodology, and the comparison belongs to a human before a season depends
on it. `official` commitment resolves its method name from the season's
frozen `SeasonRules` and refuses any name not in this registry, so an
unreviewed allocator cannot reach production by being written.

The inherited method is `ROUND_ROBIN_BY_KICKOFF_V0`, and it is kept here
for one reason: to be visible next to the alternatives. Two things are
wrong with it.

It is not a round robin. The code is
`games_by_kickoff[i % len(games)]` for `i` in `0..slots-1`, which is a
genuine rotation only when there are FEWER fixtures than slots. With ten
slots and sixteen fixtures every index is distinct and it simply takes the
first ten in kickoff order.

**At ten slots that is not a mild bias, it is total.** An NFL week has one
Thursday game and eight or nine in the early Sunday window, so the first
ten by kickoff are exactly those two blocks. Against the real 2026
schedule it drew all ten slots from 2 of Week 3's 6 kickoff blocks and 3 of
Week 4's 7, excluding every late-afternoon, Sunday-night and Monday-night
fixture in both. Every week, structurally, forever.

ARCHITECTURE.md §4a predicted this in as many words -- "with 10 slots
against a 16-game week, round-robin by kickoff ... systematically favors
Thursday/early-Sunday games and could under-select late-Sunday/Monday games
every single week" -- and called for a rule seeded only from schedule
identifiers. It was right, and the five-slot comparison understated it.

Its tie-break was a database UUID. Ten of the sixteen 2026 Week-3 fixtures
kick at exactly the same instant, so four of five slots were decided by
`str(uuid4())` ordering — reproducible inside one database and
unreproducible from the schedule.

Every allocator below is a pure function of the canonical fixture pool.
None may read a `Game`, a database id, a market, a price, or anything
about how attractive a fixture looks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Sequence

from app.forecast_lab.fixture_identity import FIXTURE_KEY_VERSION, PlannedFixture


class UnknownAllocationMethod(KeyError):
    """A method name no reviewed allocator implements."""


@dataclass(frozen=True, slots=True)
class AllocationMethod:
    name: str
    summary: str
    allocate: Callable[[Sequence[PlannedFixture], int], tuple[PlannedFixture, ...]]
    reproducible: bool
    """True when the output depends only on the fixture keys and kickoffs —
    not on input order, and not on any database identifier."""
    approved: bool = False
    """Reviewed and eligible to be frozen in `SeasonRules`. A method that is
    implemented and comparable is not thereby approved; `defect` says why
    when it is not."""
    defect: str | None = None


def _by_kickoff(fixtures: Sequence[PlannedFixture]) -> list[PlannedFixture]:
    """Kickoff order, ties broken by the CANONICAL KEY — never a UUID.

    `kickoff_at` may be None only in a preview that deliberately allowed
    an incomplete pool; those sort last so they cannot displace a fixture
    whose timing is known.
    """

    return sorted(
        fixtures,
        key=lambda f: (f.kickoff_at is None, f.kickoff_at, f.key.value),
    )


def _digest(fixture: PlannedFixture) -> str:
    """A versioned digest of the key. Version-prefixed because the ranking
    is only meaningful relative to a fixed key format."""

    return hashlib.sha256(
        f"{FIXTURE_KEY_VERSION}:{fixture.key.value}".encode("utf-8")
    ).hexdigest()


def _legacy_round_robin(
    fixtures: Sequence[PlannedFixture], slots: int
) -> tuple[PlannedFixture, ...]:
    """What Phase 2B did, reproduced exactly except for the UUID tie-break.

    Kept ONLY so the comparison shows the real incumbent. With slots <
    fixtures this takes the first `slots` fixtures by kickoff.
    """

    ordered = _by_kickoff(fixtures)
    return tuple(ordered[i % len(ordered)] for i in range(slots))


def _stratified_by_kickoff(
    fixtures: Sequence[PlannedFixture], slots: int
) -> tuple[PlannedFixture, ...]:
    """Evenly spaced RANKS across the whole week, in kickoff order.

    Rank `i` is `floor(i * n / slots)` — the midpoint-free, integer-only
    spacing that needs no rounding rule to argue about. With 16 fixtures
    and 5 slots that is ranks 0, 3, 6, 9, 12: one early, one late, three
    spread between, every week, by construction.

    Explainable in one sentence, which matters for a methodology someone
    has to defend. Its weakness is that even spacing over RANKS is not
    even spacing over TIME: ten of sixteen Week-3 fixtures share one
    kickoff instant, so three of the five ranks land inside that block.
    """

    ordered = _by_kickoff(fixtures)
    n = len(ordered)
    return tuple(ordered[(i * n) // slots] for i in range(slots))


def _stable_hash(
    fixtures: Sequence[PlannedFixture], slots: int
) -> tuple[PlannedFixture, ...]:
    """Rank every fixture by a versioned digest of its key; take the first N.

    The strongest reproducibility story here: the result depends on
    nothing but the set of keys, so input order, kickoff drift and a
    database rebuild all leave it untouched. It is also the least
    NFL-aware — a hash has no opinion about time.

    At ten slots against the real 2026 schedule it covered 5 of Week 3's 6
    kickoff blocks and 4 of Week 4's 7, missing Thursday in both. That
    looks worse than the block-stratified candidate and is a DIFFERENT KIND
    of miss: selecting 10 of 16 fixtures means each singleton block has a
    62.5% chance of inclusion, so missing some in a given week is sampling
    variance, not bias. V0 misses the same blocks every week by
    construction. A random miss and a structural one should not be compared
    on a single week's block count.

    Its pool behaviour is the best of the four: the real Week-3 provider
    gap (16 -> 14) cost it exactly one selection, removing an unselected
    fixture changes nothing, and a kickoff correction changes nothing.
    """

    return tuple(sorted(fixtures, key=_digest)[:slots])


def _kickoff_block_stratified(
    fixtures: Sequence[PlannedFixture], slots: int
) -> tuple[PlannedFixture, ...]:
    """NOT APPROVED — kept only so its defect stays visible and testable.

    Intent: deal slots round-robin across distinct kickoff blocks, then
    pick within a block by the same versioned digest `STABLE_HASH` uses.

    **The defect.** It walks blocks in chronological order and stops the
    moment `slots` selections exist. A real NFL week has six kickoff
    blocks — Thursday, the early Sunday window, two late-afternoon
    windows, Sunday night, Monday night — and five slots, so it fills
    blocks one through five and STRUCTURALLY EXCLUDES the sixth. Monday
    night can never be selected.

    Its measured "5 of 6 blocks covered" therefore describes *always the
    first five blocks*, not neutral temporal coverage. That number was
    reported in the Phase 4A.7 comparison as if it were balance, which it
    is not — the reviewer caught it, not the tests, because the test
    asserted only that it beat V0.

    **The defect does NOT manifest at the production slot count.** With ten
    slots and six or seven blocks the chronological walk completes a full
    pass before running out, so every block gets at least one selection: it
    covered 6/6 in real Week 3 and 7/7 in real Week 4. The exclusion is an
    artifact of `slots < blocks`, which is only reachable below the frozen
    slate size. Recorded honestly rather than carried forward as a
    permanent verdict.

    A second objection stands regardless of the walk order: dealing one
    slot per block weights a one-game Thursday block equally with a
    ten-game Sunday block, which heavily over-represents rare primetime
    windows relative to a neutral fixture sample.

    Deliberately NOT repaired under this name. A method that was compared
    and reported on has to keep meaning what it meant during the
    comparison; a corrected version is a new name and a new review.
    """

    blocks: dict[object, list[PlannedFixture]] = {}
    for fixture in _by_kickoff(fixtures):
        blocks.setdefault(fixture.kickoff_at, []).append(fixture)
    for candidates in blocks.values():
        candidates.sort(key=_digest)

    order = sorted(blocks, key=lambda k: (k is None, k))
    chosen: list[PlannedFixture] = []
    depth = 0
    while len(chosen) < slots:
        progressed = False
        for kickoff in order:
            if len(chosen) >= slots:
                break
            candidates = blocks[kickoff]
            if depth < len(candidates):
                chosen.append(candidates[depth])
                progressed = True
        if not progressed:  # pragma: no cover - slots > fixtures guard
            break
        depth += 1
    return tuple(chosen)


ALLOCATION_METHODS: dict[str, AllocationMethod] = {
    "ROUND_ROBIN_BY_KICKOFF_V0": AllocationMethod(
        name="ROUND_ROBIN_BY_KICKOFF_V0",
        summary="Inherited. Takes the first N fixtures by kickoff; not a round "
                "robin when slots < fixtures.",
        allocate=_legacy_round_robin,
        reproducible=True,
        approved=False,
        defect="Not a round robin when slots < fixtures: it takes the first N "
               "by kickoff. At the production slot count of 10 that is TOTAL "
               "temporal bias -- all ten from 2 of 6 kickoff blocks in real "
               "Week 3 and 3 of 7 in Week 4, excluding every late-afternoon, "
               "Sunday-night and Monday-night fixture in both. REJECTED.",
    ),
    "STRATIFIED_BY_KICKOFF_V1": AllocationMethod(
        name="STRATIFIED_BY_KICKOFF_V1",
        summary="Evenly spaced ranks across the kickoff-ordered week "
                "(floor(i*n/slots)).",
        allocate=_stratified_by_kickoff,
        reproducible=True,
        approved=False,
        defect="Samples POSITIONS rather than fixtures, so changing the pool "
               "size moves every rank: at 10 slots, the real Week-3 provider "
               "gap (16 -> 14) replaced three of ten selections, and removing "
               "one selected fixture cascaded into four further changes. It "
               "is also the only candidate whose selection moves when only a "
               "KICKOFF changes. Not selected.",
    ),
    "STABLE_HASH_V1": AllocationMethod(
        name="STABLE_HASH_V1",
        summary="Rank all fixture keys by a versioned digest, take the first N. "
                "Fixture-symmetric: no kickoff-time weighting of any kind.",
        allocate=_stable_hash,
        reproducible=True,
        approved=True,
    ),
    "KICKOFF_BLOCK_STRATIFIED_V1": AllocationMethod(
        name="KICKOFF_BLOCK_STRATIFIED_V1",
        summary="EXPERIMENTAL. Deals slots across distinct kickoff blocks in "
                "chronological order.",
        allocate=_kickoff_block_stratified,
        reproducible=True,
        approved=False,
        defect="Structurally excludes the latest kickoff block whenever "
               "slots < blocks (six blocks, five slots -> Monday night can "
               "never be selected). At the production count of 10 that does "
               "NOT occur -- it covered 6/6 and 7/7 blocks on the real 2026 "
               "weeks. The standing objection is weighting: one slot per "
               "block deals a singleton Monday-night block the same share as "
               "a nine-game Sunday block, so a primetime fixture is roughly "
               "three times likelier to be benchmarked than an early one. "
               "Deliberate over-representation, not neutrality.",
    ),
}

APPROVED_METHODS = frozenset(
    name for name, m in ALLOCATION_METHODS.items() if m.approved
)
"""Eligible to be frozen in `SeasonRules`. Being implemented and
comparable is not approval — every other method here was written to be
measured against this one, and two of them have named defects."""


def allocate(
    fixtures: Sequence[PlannedFixture], *, slots: int, method: str
) -> tuple[PlannedFixture, ...]:
    """Choose `slots` fixtures from the pool by a named, reviewed method."""

    if slots < 1:
        raise ValueError(f"a slate needs at least one slot, got {slots}")
    if not fixtures:
        raise ValueError("cannot allocate a slate from an empty fixture pool")
    implementation = ALLOCATION_METHODS.get(method)
    if implementation is None:
        raise UnknownAllocationMethod(
            f"{method!r} is not a reviewed allocation method. Known: "
            + ", ".join(sorted(ALLOCATION_METHODS))
        )
    chosen = implementation.allocate(fixtures, min(slots, len(fixtures)))
    keys = [f.key.value for f in chosen]
    if len(set(keys)) != len(keys):
        raise RuntimeError(
            f"{method} selected the same fixture twice ({keys}); an allocator "
            "must never double-count a fixture into two slots"
        )
    return chosen
