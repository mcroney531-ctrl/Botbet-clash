"""How five benchmark slots are chosen from a sixteen-fixture week. Pure.

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
genuine rotation only when there are FEWER fixtures than slots. With five
slots and sixteen fixtures every index is distinct and it simply takes the
first five in sort order — a pure recency bias toward whichever games kick
earliest, which in an NFL week means Thursday night and the early Sunday
block, every week, forever.

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
    NFL-aware — a hash has no opinion about time, so it can legitimately
    put four of five slots in the same kickoff block, or none in the late
    window at all.
    """

    return tuple(sorted(fixtures, key=_digest)[:slots])


def _kickoff_block_stratified(
    fixtures: Sequence[PlannedFixture], slots: int
) -> tuple[PlannedFixture, ...]:
    """Spread across DISTINCT kickoff blocks first, then hash within a block.

    Blocks are distinct kickoff instants in time order. Slots are dealt
    round-robin across blocks — one per block until every block has one,
    then a second pass — and within a block the next fixture is taken by
    the same versioned digest `STABLE_HASH` uses.

    The most NFL-aware of the three: a week with a Thursday game, three
    Sunday blocks and a Monday game gets genuine temporal coverage rather
    than five games from whichever block happens to be largest. It is also
    the most methodology to defend, and it weights a one-game Thursday
    block equally with a ten-game Sunday block, which is a real choice and
    not obviously the right one.
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
                "robin when slots < fixtures. Kept for comparison only.",
        allocate=_legacy_round_robin,
        reproducible=True,
    ),
    "STRATIFIED_BY_KICKOFF_V1": AllocationMethod(
        name="STRATIFIED_BY_KICKOFF_V1",
        summary="Evenly spaced ranks across the kickoff-ordered week "
                "(floor(i*n/slots)). Most explainable.",
        allocate=_stratified_by_kickoff,
        reproducible=True,
    ),
    "STABLE_HASH_V1": AllocationMethod(
        name="STABLE_HASH_V1",
        summary="Rank all fixture keys by a versioned digest, take the first N. "
                "Strongest reproducibility, no time awareness.",
        allocate=_stable_hash,
        reproducible=True,
    ),
    "KICKOFF_BLOCK_STRATIFIED_V1": AllocationMethod(
        name="KICKOFF_BLOCK_STRATIFIED_V1",
        summary="Deal slots round-robin across distinct kickoff blocks, then "
                "pick within a block by digest. Most NFL-aware.",
        allocate=_kickoff_block_stratified,
        reproducible=True,
    ),
}


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
