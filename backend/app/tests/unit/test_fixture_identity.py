"""Fixture identity and allocation. Pure — no database, no network.

The failure being killed: a benchmark slate that depends on `uuid.uuid4()`
and on whether the odds provider had posted an event yet. Both make a
"precommitted" research sample unreproducible from its own inputs.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.forecast_lab.fixture_identity import (
    FIXTURE_KEY_VERSION,
    FixtureKey,
    FixtureKeyError,
    IncompletePool,
    PlannedFixture,
    planned_pool,
    pool_fingerprint,
)
from app.forecast_lab.slate_allocation import (
    ALLOCATION_METHODS,
    UnknownAllocationMethod,
    allocate,
)
from app.rosterdata.teams import CanonicalTeam
from app.scheduledata.base import ScheduledGame, ScheduleSnapshot

SUNDAY = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def _key(away, home, week=3):
    return FixtureKey(
        season=2026, game_type="REG", week=week,
        away=CanonicalTeam(away), home=CanonicalTeam(home),
    )


def _fixture(away, home, *, week=3, kickoff=SUNDAY):
    return PlannedFixture(key=_key(away, home, week), kickoff_at=kickoff)


def _snapshot(*games):
    return ScheduleSnapshot(
        provider="NFLVERSE", season=2026, retrieved_at=SUNDAY, games=tuple(games),
    )


def _scheduled(away, home, week, kickoff=SUNDAY, game_type="REG"):
    return ScheduledGame(
        season=2026, week=week, game_type=game_type,
        home=CanonicalTeam(home), away=CanonicalTeam(away), kickoff_at=kickoff,
    )


# --- identity ---------------------------------------------------------


def test_the_key_is_built_from_five_schedule_facts():
    assert _key("ATL", "GB").value == "2026:REG:W03:ATL@GB"


def test_the_key_round_trips():
    key = _key("ATL", "GB")
    assert FixtureKey.parse(key.value) == key


def test_the_week_is_zero_padded_so_string_order_is_numeric_order():
    """Without padding, week 10 sorts before week 2 and every ordering
    built on keys is quietly wrong past week 9."""

    keys = sorted(_key("ATL", "GB", w).value for w in (2, 10, 3))
    assert keys == [
        "2026:REG:W02:ATL@GB", "2026:REG:W03:ATL@GB", "2026:REG:W10:ATL@GB",
    ]


def test_kickoff_is_not_part_of_identity():
    """Broadcast times move; the fixture does not. An identity that changed
    with the clock would silently un-bind a committed plan."""

    early = _fixture("ATL", "GB", kickoff=SUNDAY)
    flexed = _fixture("ATL", "GB", kickoff=SUNDAY + timedelta(hours=3))
    assert early.key == flexed.key


def test_the_key_type_has_no_kickoff_field_at_all():
    """Structural, not behavioural. Kickoff staying out of identity is a
    property of the TYPE -- if a field were ever added, every key would
    silently start moving with the broadcast schedule."""

    import dataclasses

    fields = {f.name for f in dataclasses.fields(FixtureKey)}
    assert fields == {"season", "game_type", "week", "away", "home"}


def test_the_hash_ranking_is_version_prefixed():
    """`STABLE_HASH_V1` ranks by a digest of the key, so the key FORMAT is
    methodology: an unversioned digest would let a spelling change
    reshuffle a hash-ranked slate with nobody touching the allocator."""

    import hashlib

    from app.forecast_lab.slate_allocation import _digest

    fixture = _fixture("ATL", "GB")
    bare = hashlib.sha256(fixture.key.value.encode("utf-8")).hexdigest()
    versioned = hashlib.sha256(
        f"{FIXTURE_KEY_VERSION}:{fixture.key.value}".encode("utf-8")
    ).hexdigest()
    assert _digest(fixture) == versioned
    assert _digest(fixture) != bare


def test_an_allocator_that_double_counts_is_caught():
    """Defence in depth: no reviewed allocator does this, which is exactly
    why the guard needs a test of its own rather than relying on one of
    them going wrong."""

    from app.forecast_lab import slate_allocation
    from app.forecast_lab.slate_allocation import ALLOCATION_METHODS, AllocationMethod

    broken = AllocationMethod(
        name="BROKEN", summary="picks the first fixture five times",
        allocate=lambda fixtures, slots: tuple([fixtures[0]] * slots),
        reproducible=True,
    )
    ALLOCATION_METHODS["BROKEN_FOR_TEST"] = broken
    try:
        with pytest.raises(RuntimeError, match="same fixture twice"):
            allocate(_week_pool(), slots=5, method="BROKEN_FOR_TEST")
    finally:
        del ALLOCATION_METHODS["BROKEN_FOR_TEST"]


def test_the_reverse_fixture_is_a_different_key():
    assert _key("ATL", "GB").value != _key("GB", "ATL").value


def test_the_week_free_pair_matches_what_resolve_fixture_searches_on():
    assert _key("ATL", "GB", 3).pair == _key("ATL", "GB", 11).pair


def test_a_team_cannot_host_itself():
    with pytest.raises(FixtureKeyError):
        _key("GB", "GB")


def test_garbage_is_not_parsed_into_a_key():
    for bad in ("", "ATL@GB", "2026:REG:W3:ATL@GB", "2026:REG:W03:XXX@GB"):
        with pytest.raises(FixtureKeyError):
            FixtureKey.parse(bad)


# --- the pool ---------------------------------------------------------


def test_the_pool_is_every_fixture_in_the_week_in_key_order():
    pool = planned_pool(
        _snapshot(
            _scheduled("NE", "JAX", 3), _scheduled("ATL", "GB", 3),
            _scheduled("KC", "MIA", 4),
        ),
        week=3,
    )
    assert [f.key.value for f in pool] == ["2026:REG:W03:ATL@GB", "2026:REG:W03:NE@JAX"]


def test_a_missing_kickoff_is_fatal_not_skipped():
    """The commit deadline is the earliest OPENING start and OPENING is
    computed from kickoff, so one unknown kickoff makes the deadline
    uncomputable. A deadline you cannot compute cannot be enforced."""

    with pytest.raises(IncompletePool, match="no kickoff time"):
        planned_pool(
            _snapshot(_scheduled("ATL", "GB", 3), _scheduled("NE", "JAX", 3, kickoff=None)),
            week=3,
        )


def test_postseason_fixtures_are_not_in_a_regular_season_pool():
    pool = planned_pool(
        _snapshot(_scheduled("ATL", "GB", 3), _scheduled("KC", "MIA", 3, game_type="POST")),
        week=3,
    )
    assert [f.key.value for f in pool] == ["2026:REG:W03:ATL@GB"]


# --- the fingerprint --------------------------------------------------


def test_the_fingerprint_ignores_input_order():
    a = [_fixture("ATL", "GB"), _fixture("NE", "JAX"), _fixture("KC", "MIA")]
    assert pool_fingerprint(a) == pool_fingerprint(list(reversed(a)))


def test_the_fingerprint_ignores_kickoff_drift():
    """A flexed broadcast time is the same pool. If the fingerprint moved,
    every plan would look tampered with whenever a game was rescheduled."""

    a = [_fixture("ATL", "GB", kickoff=SUNDAY)]
    b = [_fixture("ATL", "GB", kickoff=SUNDAY + timedelta(hours=3))]
    assert pool_fingerprint(a) == pool_fingerprint(b)


def test_the_fingerprint_changes_when_the_pool_changes():
    a = [_fixture("ATL", "GB"), _fixture("NE", "JAX")]
    assert pool_fingerprint(a) != pool_fingerprint(a[:1])


def test_the_fingerprint_is_version_prefixed():
    import hashlib

    fixture = _fixture("ATL", "GB")
    naked = hashlib.sha256(fixture.key.value.encode()).hexdigest()
    assert pool_fingerprint([fixture]) != naked, "the key version is not in the digest"
    assert FIXTURE_KEY_VERSION


# --- allocation -------------------------------------------------------


def _week_pool():
    """A realistic NFL week: one Thursday, ten in the early Sunday block,
    two late-afternoon, one Sunday night, one Monday. This shape is why
    the UUID tie-break mattered — most of the week kicks at one instant."""

    early = [
        _fixture(a, h) for a, h in (
            ("CAR", "CLE"), ("CIN", "PIT"), ("HOU", "IND"), ("NE", "JAX"),
            ("KC", "MIA"), ("TEN", "NYG"), ("SEA", "WAS"), ("LAC", "BUF"),
            ("NYJ", "DET"), ("MIN", "TB"),
        )
    ]
    return [
        _fixture("ATL", "GB", kickoff=SUNDAY - timedelta(days=2)),
        *early,
        _fixture("ARI", "SF", kickoff=SUNDAY + timedelta(hours=3)),
        _fixture("BAL", "DAL", kickoff=SUNDAY + timedelta(hours=3, minutes=25)),
        _fixture("LA", "DEN", kickoff=SUNDAY + timedelta(hours=7)),
        _fixture("PHI", "CHI", kickoff=SUNDAY + timedelta(days=1, hours=7)),
    ]


@pytest.mark.parametrize("method", sorted(ALLOCATION_METHODS))
def test_no_allocator_reads_input_order_or_a_database_id(method):
    """THE headline property. Shuffle the pool a thousand ways — which is
    what a different insertion order or a clean database rebuild looks
    like — and the slate must be byte-for-byte identical."""

    pool = _week_pool()
    expected = [f.key.value for f in allocate(pool, slots=5, method=method)]
    fingerprint = pool_fingerprint(pool)
    rng = random.Random(1861)
    for _ in range(1000):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        assert [f.key.value for f in allocate(shuffled, slots=5, method=method)] == expected
        assert pool_fingerprint(shuffled) == fingerprint


@pytest.mark.parametrize("method", sorted(ALLOCATION_METHODS))
def test_no_allocator_picks_the_same_fixture_twice(method):
    chosen = allocate(_week_pool(), slots=5, method=method)
    assert len({f.key.value for f in chosen}) == 5


@pytest.mark.parametrize("method", sorted(ALLOCATION_METHODS))
def test_every_allocator_picks_from_the_pool_it_was_given(method):
    pool = _week_pool()
    keys = {f.key.value for f in pool}
    assert all(f.key.value in keys for f in allocate(pool, slots=5, method=method))


def test_the_inherited_v0_is_not_a_round_robin():
    """`games[i % len(games)]` with five slots and sixteen fixtures is
    every index distinct — it takes the first five by kickoff, which is a
    permanent bias toward whichever games kick earliest."""

    pool = _week_pool()
    chosen = allocate(pool, slots=5, method="ROUND_ROBIN_BY_KICKOFF_V0")
    by_kickoff = sorted(pool, key=lambda f: (f.kickoff_at, f.key.value))
    assert [f.key.value for f in chosen] == [f.key.value for f in by_kickoff[:5]]
    blocks = {f.kickoff_at for f in chosen}
    assert len(blocks) <= 2, "V0 unexpectedly spread across the week"


def test_the_block_stratified_candidate_covers_more_of_the_week_than_v0():
    pool = _week_pool()
    v0 = {f.kickoff_at for f in allocate(pool, slots=5, method="ROUND_ROBIN_BY_KICKOFF_V0")}
    v1 = {
        f.kickoff_at
        for f in allocate(pool, slots=5, method="KICKOFF_BLOCK_STRATIFIED_V1")
    }
    assert len(v1) > len(v0)


def test_an_unreviewed_method_name_is_refused():
    with pytest.raises(UnknownAllocationMethod):
        allocate(_week_pool(), slots=5, method="WHATEVER_LOOKS_GOOD_V9")


def test_a_pool_smaller_than_the_slate_does_not_double_count():
    pool = [_fixture("ATL", "GB"), _fixture("NE", "JAX")]
    for method in sorted(ALLOCATION_METHODS):
        chosen = allocate(pool, slots=5, method=method)
        assert len({f.key.value for f in chosen}) == len(chosen) <= 2


def test_an_empty_pool_is_refused():
    with pytest.raises(ValueError, match="empty fixture pool"):
        allocate([], slots=5, method="STABLE_HASH_V1")
