"""Phase 4A.3 — per-book observation freshness and selection provenance.

These run against real Postgres but spend ZERO Odds API credits. The two
observation timestamps are the actual `as_of_at` values written by the two
real DET @ BUF ingestion runs, and the canonical price pair is the one
those runs produced; the second book's numbers are constructed, because
what is under test is the SELECTION rule, not the arithmetic (that already
has its own tests).

Using the real timestamps matters: the gap between the two runs is nearly
sixteen hours, which is exactly the regime a live FINAL-window capture has
to make a decision about. A fixture of "now" and "now minus ten seconds"
would pass without ever exercising a realistic staleness spread.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.ingestion import ProviderCall
from app.db.models.markets import CheckpointRun, Game, MarketSnapshot, PropMarket, PropQuote
from app.db.models.season import Season
from app.db.repositories.market_repository import MarketRepository
from app.db.session import session_scope
from app.domain.enums import StatFamily
from app.forecast_lab.checkpoint_service import capture_checkpoint
from app.forecast_lab.market_snapshot_service import MarketSnapshotService
from app.marketdata.base import ProviderCallMetadata
from app.marketdata.dto import ProviderEventRef, ProviderPlayerRef, ProviderQuote
from app.marketdata.ingestion import IngestionService
from app.marketdata.telemetry import record_call, start_run

PROVIDER = "THE_ODDS_API"
CANONICAL = "DRAFTKINGS"

# The real observation times, copied from the two live acceptance runs.
OBS_EARLY = datetime(2026, 9, 17, 4, 19, 20, 12177, tzinfo=timezone.utc)
OBS_LATE = datetime(2026, 9, 17, 20, 10, 56, 225032, tzinfo=timezone.utc)
REAL_GAP_SECONDS = 57096.212855

# The canonical pair one of those real snapshots actually carried.
REAL_LINE = Decimal("74.5")
REAL_OVER = -165
REAL_UNDER = 129

WINDOWS = {
    "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
    "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
    "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
}


def test_the_two_real_observation_times_are_the_gap_these_tests_claim():
    """Guards the constants above. If someone "tidies" the microseconds off
    these timestamps the rest of the file still passes while no longer
    testing the interval it says it tests."""

    assert (OBS_LATE - OBS_EARLY).total_seconds() == pytest.approx(REAL_GAP_SECONDS, abs=1e-6)
    assert REAL_GAP_SECONDS > 15 * 3600


# --- fixtures ---------------------------------------------------------


def _market(session, tag: str, *, kickoff_at: datetime | None = None) -> PropMarket:
    season = Season(year=2026, name=f"fresh-{tag}", status="ACTIVE")
    session.add(season)
    session.flush()
    game = Game(
        external_ref=f"fresh-{tag}",
        season_id=season.id,
        week_number=3,
        home_team="Buffalo Bills",
        away_team="Detroit Lions",
        home_team_canonical="BUF",
        away_team_canonical="DET",
        kickoff_at=kickoff_at or (OBS_LATE + timedelta(hours=4)),
    )
    session.add(game)
    session.flush()
    player = _player(session, tag)
    market = PropMarket(game_id=game.id, player_id=player, stat_type="receiving_yards")
    session.add(market)
    session.flush()
    return market


def _player(session, tag: str) -> uuid.UUID:
    from app.db.models.markets import Player

    row = Player(external_ref=f"GSIS:fresh-{tag}", name=f"Player {tag}")
    session.add(row)
    session.flush()
    return row.id


def _call(session, *, tag: str) -> ProviderCall:
    """One provider call per simulated poll.

    Not a detail: the quote fingerprint is keyed on the call id, so two
    observations of the same unchanged price at different times are only
    distinguishable -- and only both persistable -- because each poll is
    its own call. Reusing one call here would silently dedup the second
    observation away and the freshness tests would be reading a single row.
    """

    run = start_run(session, provider=PROVIDER, operation="FETCH_QUOTES")
    return record_call(
        session,
        run=run,
        metadata=ProviderCallMetadata(
            endpoint_capability="FETCH_EVENT_ODDS",
            requested_at=OBS_EARLY,
            responded_at=OBS_EARLY,
            http_status=200,
            raw_response_body=f"json-{tag}".encode(),
            raw_response_sha256="c" * 64,
            raw_response_bytes=12,
        ),
        success=True,
    )


def _quote(
    session,
    *,
    market: PropMarket,
    sportsbook: str,
    as_of_at: datetime,
    line: Decimal = REAL_LINE,
    over: int = REAL_OVER,
    under: int = REAL_UNDER,
    provider_market_updated_at: datetime | None = None,
    tag: str | None = None,
) -> PropQuote:
    call = _call(session, tag=tag or f"{sportsbook}-{as_of_at.isoformat()}")
    row = IngestionService(session).persist_quote(
        quote=ProviderQuote(
            event=ProviderEventRef(provider=PROVIDER, external_event_id=f"e-{market.id}"),
            player=ProviderPlayerRef(provider=PROVIDER, display_name="Player X"),
            stat_family=StatFamily.RECEIVING_YARDS,
            vendor_market_key="player_reception_yds",
            sportsbook=sportsbook,
            line=line,
            over_price=over,
            under_price=under,
            as_of_at=as_of_at,
            retrieved_at=as_of_at,
            provider_market_updated_at=provider_market_updated_at,
            source=PROVIDER,
        ),
        market_id=market.id,
        provider_call=call,
    )
    assert row is not None, "fixture wrote a duplicate fingerprint"
    return row


def _service(max_age: int | None) -> dict:
    return {"market_data_provider": PROVIDER, "max_observation_age_seconds": max_age}


# --- no gate ----------------------------------------------------------


def test_no_gate_excludes_nothing_but_still_records_what_was_consumed():
    """`max_observation_age_seconds=None` is the default and must keep the
    pre-4A.3 behaviour exactly: a sixteen-hour-old observation is still
    consumed. The selection record is written regardless, because
    provenance is not conditional on a rule being in force."""

    with session_scope() as session:
        market = _market(session, "nogate")
        dk = _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_EARLY)
        fd = _quote(session, market=market, sportsbook="FANDUEL", as_of_at=OBS_EARLY)

        snap = MarketSnapshotService(session, **_service(None)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        assert snap.number_of_books == 2
        assert snap.is_valid_canonical_baseline is True
        assert snap.max_observation_age_seconds is None
        assert snap.stale_books_excluded == 0
        assert snap.canonical_quote_stale is False

        record = snap.selected_quotes
        assert record["schema"] == "market_snapshot_selection_v1"
        assert record["excluded_stale"] == []
        assert {e["quote_id"] for e in record["included"]} == {str(dk.id), str(fd.id)}
        for entry in record["included"]:
            assert entry["observation_age_seconds"] == pytest.approx(REAL_GAP_SECONDS, abs=1e-3)
        assert [e["is_canonical"] for e in record["included"] if e["sportsbook"] == CANONICAL] == [True]


# --- a stale non-canonical book ---------------------------------------


def test_a_stale_non_canonical_book_leaves_consensus_and_line_context():
    """Excluded means excluded from EVERYTHING the snapshot reports, not
    just from the consensus probability. A book whose price we last saw
    sixteen hours ago must not widen market_min/max_line either -- that
    context is read as "what the market looks like right now"."""

    with session_scope() as session:
        market = _market(session, "stale-noncanon")
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_LATE)
        fd = _quote(
            session, market=market, sportsbook="FANDUEL",
            as_of_at=OBS_EARLY, line=Decimal("69.5"), over=-120, under=-110,
        )
        fd_id, fd_line, fd_as_of = fd.id, fd.line, fd.as_of_at

        snap = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        assert snap.number_of_books == 1
        assert snap.stale_books_excluded == 1
        assert snap.canonical_quote_stale is False
        assert snap.is_valid_canonical_baseline is True
        assert snap.max_observation_age_seconds == 3600

        # FanDuel's 69.5 is nowhere in the reported market context.
        assert snap.market_min_line == REAL_LINE
        assert snap.market_max_line == REAL_LINE
        assert snap.market_median_line == REAL_LINE

        assert [e["quote_id"] for e in snap.selected_quotes["excluded_stale"]] == [str(fd_id)]

    # The quote row itself is untouched. Exclusion is a snapshot decision,
    # never a retraction of an observation we genuinely made.
    with session_scope() as session:
        still_there = session.get(PropQuote, fd_id)
        assert still_there is not None
        assert still_there.line == fd_line and still_there.as_of_at == fd_as_of


# --- a stale canonical book -------------------------------------------


def test_a_stale_canonical_book_invalidates_the_baseline_and_promotes_nobody():
    """RULES.md §16: if the canonical market is unavailable, do not
    silently substitute. "Too old to trust" is a form of unavailable, and
    two perfectly fresh books sitting right there is exactly the situation
    where substitution is tempting."""

    with session_scope() as session:
        market = _market(session, "stale-canon")
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_EARLY)
        _quote(session, market=market, sportsbook="FANDUEL", as_of_at=OBS_LATE, line=Decimal("73.5"))
        _quote(session, market=market, sportsbook="CAESARS", as_of_at=OBS_LATE, line=Decimal("75.5"))

        snap = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        assert snap.canonical_quote_stale is True
        assert snap.is_valid_canonical_baseline is False
        assert snap.canonical_sportsbook == CANONICAL, "the canonical book is never swapped"
        assert snap.canonical_line is None
        assert snap.canonical_over_price is None and snap.canonical_under_price is None
        assert snap.canonical_over_probability is None
        assert snap.same_line_consensus_over_probability is None

        # The fresh books still provide market context -- that is diagnostic,
        # not a baseline, so losing canonical does not have to blind us.
        assert snap.number_of_books == 2
        assert snap.market_min_line == Decimal("73.50")
        assert snap.market_max_line == Decimal("75.50")
        assert snap.stale_books_excluded == 1


def test_canonical_stale_is_distinguishable_from_canonical_absent():
    """Both invalidate the baseline; they are different failures. One says
    the book stopped being quoted, the other says our feed went quiet. A
    single `is_valid_canonical_baseline=False` cannot tell them apart, and
    only one of them is an ingestion problem."""

    with session_scope() as session:
        absent = _market(session, "canon-absent")
        _quote(session, market=absent, sportsbook="FANDUEL", as_of_at=OBS_LATE)
        snap_absent = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=absent.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        stale = _market(session, "canon-stale")
        _quote(session, market=stale, sportsbook=CANONICAL, as_of_at=OBS_EARLY)
        snap_stale = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=stale.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        assert snap_absent.is_valid_canonical_baseline is False
        assert snap_stale.is_valid_canonical_baseline is False
        assert snap_absent.canonical_quote_stale is False
        assert snap_stale.canonical_quote_stale is True


# --- the metric itself ------------------------------------------------


def test_freshness_never_reads_provider_market_updated_at():
    """The methodology error this phase exists to prevent.

    Book A was observed this instant but has not MOVED in ten hours.
    Book B was last observed sixteen hours ago but the vendor says its
    market changed a second ago. If eligibility read the vendor field,
    both verdicts would flip: the quiet-but-current book would be thrown
    out and the long-lost one kept.
    """

    with session_scope() as session:
        market = _market(session, "vendor-field")
        quiet = _quote(
            session, market=market, sportsbook=CANONICAL, as_of_at=OBS_LATE,
            provider_market_updated_at=OBS_LATE - timedelta(hours=10),
        )
        lost = _quote(
            session, market=market, sportsbook="FANDUEL", as_of_at=OBS_EARLY,
            provider_market_updated_at=OBS_LATE - timedelta(seconds=1),
        )

        snap = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        included = {e["quote_id"] for e in snap.selected_quotes["included"]}
        excluded = {e["quote_id"] for e in snap.selected_quotes["excluded_stale"]}
        assert included == {str(quiet.id)}
        assert excluded == {str(lost.id)}
        assert snap.is_valid_canonical_baseline is True


def test_the_age_boundary_is_inclusive():
    """An age exactly equal to the configured maximum is fresh. Stated as a
    test because "> max" vs ">= max" is a one-character difference that
    silently changes which observations a whole season consumed."""

    exact = int(REAL_GAP_SECONDS)  # floor: the real age is fractionally more

    with session_scope() as session:
        market = _market(session, "boundary")
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_EARLY)

        # taken_at set so the age is exactly `exact` seconds.
        at_limit = OBS_EARLY + timedelta(seconds=exact)
        just_over = OBS_EARLY + timedelta(seconds=exact + 1)

        inside = MarketSnapshotService(session, **_service(exact)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=at_limit
        )
        outside = MarketSnapshotService(session, **_service(exact)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=just_over
        )

        assert inside.number_of_books == 1 and inside.stale_books_excluded == 0
        assert outside.number_of_books == 0 and outside.stale_books_excluded == 1
        assert outside.canonical_quote_stale is True


def test_every_observed_book_is_accounted_for_as_included_or_excluded():
    """number_of_books + stale_books_excluded == books that had a quote.
    A book must never fall out of both counts: that is how "the feed
    dropped a book" turns into a snapshot that looks merely thin."""

    with session_scope() as session:
        market = _market(session, "accounting")
        for book, at in (
            (CANONICAL, OBS_LATE),
            ("FANDUEL", OBS_LATE),
            ("CAESARS", OBS_EARLY),
            ("BETMGM", OBS_EARLY),
        ):
            _quote(session, market=market, sportsbook=book, as_of_at=at)

        snap = MarketSnapshotService(session, **_service(3600)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

        assert snap.number_of_books + snap.stale_books_excluded == 4
        record = snap.selected_quotes
        assert len(record["included"]) == snap.number_of_books
        assert len(record["excluded_stale"]) == snap.stale_books_excluded


# --- the selection record is an artifact, not a query ------------------


def test_the_selection_record_survives_a_later_backfill():
    """Why the ids are frozen into the row instead of re-derived.

    `quotes_as_of` filters `as_of_at <= taken_at`, which LOOKS
    reproducible. It is not: a historical backfill can legitimately insert
    an observation whose as_of_at predates a snapshot already taken, and
    the same query then returns a different answer for the same snapshot.
    """

    with session_scope() as session:
        market = _market(session, "backfill")
        original = _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_LATE)
        snap = MarketSnapshotService(session, **_service(None)).build_snapshot(
            market_id=market.id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )
        market_id, snapshot_id, original_id = market.id, snap.id, original.id

    with session_scope() as session:
        market = session.get(PropMarket, market_id)
        _quote(
            session, market=market, sportsbook="FANDUEL",
            as_of_at=OBS_LATE - timedelta(minutes=5), line=Decimal("70.5"), tag="late-backfill",
        )

    with session_scope() as session:
        re_derived = MarketRepository(session).quotes_as_of(
            market_id, source=PROVIDER, as_of=OBS_LATE
        )
        assert len(re_derived) == 2, "the backfill IS visible to the same query"

        stored = session.get(MarketSnapshot, snapshot_id)
        assert [e["quote_id"] for e in stored.selected_quotes["included"]] == [str(original_id)]
        assert stored.number_of_books == 1


# --- through the checkpoint path --------------------------------------


def test_capture_checkpoint_measures_age_against_captured_at_not_target_time():
    """The correction that opened this phase.

    `target_time` is scheduling INTENT -- where we wanted to land. A
    capture that fires late is still a real capture, and the observations
    it consumed are only stale relative to when it actually ran. Measuring
    against target_time would report the scheduler's lateness as market
    staleness and exclude quotes that were fresh at capture.
    """

    kickoff = OBS_LATE + timedelta(hours=4)  # inside the FINAL window (6h..2h)

    with session_scope() as session:
        market = _market(session, "checkpoint", kickoff_at=kickoff)
        game = session.get(Game, market.game_id)
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_LATE)
        _quote(session, market=market, sportsbook="FANDUEL", as_of_at=OBS_EARLY)

        run = capture_checkpoint(
            session,
            game=game,
            checkpoint_type="FINAL",
            windows_config=WINDOWS,
            now=OBS_LATE,
            canonical_sportsbook=CANONICAL,
            market_data_provider=PROVIDER,
            max_observation_age_seconds=3600,
        )
        assert run.status == "CAPTURED"
        run_id, market_id = run.id, market.id

    with session_scope() as session:
        run = session.get(CheckpointRun, run_id)
        snap = MarketRepository(session).latest_snapshot(market_id)

        # target_time (kickoff - 3h) is an hour away from captured_at.
        assert run.target_time != run.captured_at
        assert snap.taken_at == run.captured_at

        # Against captured_at the canonical quote is zero seconds old and
        # kept. Against target_time it would be an hour "in the future",
        # and the stale FanDuel quote's age would be an hour different.
        ages = {e["sportsbook"]: e["observation_age_seconds"] for e in snap.selected_quotes["included"]}
        assert ages == {CANONICAL: 0.0}
        assert snap.is_valid_canonical_baseline is True
        assert snap.stale_books_excluded == 1


def test_capture_checkpoint_defaults_to_no_gate():
    """A caller that says nothing gets the Phase-2 behaviour. The gate is
    opt-in until a real number exists to put in SeasonRules; defaulting to
    a plausible-looking one would freeze a guess into the record."""

    kickoff = OBS_LATE + timedelta(hours=4)

    with session_scope() as session:
        market = _market(session, "checkpoint-default", kickoff_at=kickoff)
        game = session.get(Game, market.game_id)
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_EARLY)

        capture_checkpoint(
            session,
            game=game,
            checkpoint_type="FINAL",
            windows_config=WINDOWS,
            now=OBS_LATE,
            canonical_sportsbook=CANONICAL,
            market_data_provider=PROVIDER,
        )
        market_id = market.id

    with session_scope() as session:
        snap = MarketRepository(session).latest_snapshot(market_id)
        assert snap.max_observation_age_seconds is None
        assert snap.stale_books_excluded == 0
        assert snap.is_valid_canonical_baseline is True


# --- the gate is not a filter on anything else -------------------------


def test_the_gate_does_not_touch_the_quote_table():
    """Phase 4A.3 adds a READ-side rule. It must not delete, flag, or
    otherwise mutate PropQuote -- the observation record is append-only and
    a freshness threshold is a decision we may want to revisit."""

    with session_scope() as session:
        market = _market(session, "readonly")
        _quote(session, market=market, sportsbook=CANONICAL, as_of_at=OBS_EARLY)
        _quote(session, market=market, sportsbook="FANDUEL", as_of_at=OBS_EARLY)
        market_id = market.id

    with session_scope() as session:
        before = sorted(
            (q.id, q.sportsbook, q.line, q.as_of_at, q.fingerprint)
            for q in session.execute(select(PropQuote).where(PropQuote.market_id == market_id)).scalars()
        )

    with session_scope() as session:
        MarketSnapshotService(session, **_service(1)).build_snapshot(
            market_id=market_id, canonical_sportsbook=CANONICAL, taken_at=OBS_LATE
        )

    with session_scope() as session:
        after = sorted(
            (q.id, q.sportsbook, q.line, q.as_of_at, q.fingerprint)
            for q in session.execute(select(PropQuote).where(PropQuote.market_id == market_id)).scalars()
        )

    assert before == after
