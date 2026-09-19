"""Preparing a week is not opening it.

`BenchmarkSlatePlan.week_id` must exist BEFORE the first OPENING window,
because the slate is precommitted. The only persistence path that made a
Week row was `open_week`, which creates it already OPENED and emits
`WEEK_OPENED` — so precommitting the research sample required declaring the
competition week open first, purely as a schema side effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.events import CompetitionEvent
from app.db.models.markets import Game
from app.db.models.season import Season, SeasonRules, Week
from app.db.session import session_scope
from app.domain.week_profile import WEEK_PROFILES, WeekFlags, WeekProfile, profile_of
from app.services.prepare_week import (
    WeekPreparationRefused,
    apply_preparation,
    main,
    plan_preparation,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _season(tag):
    with session_scope() as session:
        season = Season(year=2026, name=f"prep-{tag}", status="ACTIVE")
        session.add(season)
        session.flush()
        session.add(SeasonRules(
            season_id=season.id, rules_version=f"prep-{tag}-{uuid.uuid4()}",
            starting_bankroll_cents=1500, canonical_sportsbook="DRAFTKINGS",
            market_data_provider="THE_ODDS_API", roster_data_provider="NFLVERSE",
            research_settlement_provider="NFLVERSE", research_settlement_delay_hours=24,
            supported_prop_types=["receiving_yards"], devig_method="PROPORTIONAL_V1",
            benchmark_slate_size=5, batch_methodology="SINGLE_BATCH",
            checkpoint_windows={
                "OPENING": {"start_hours_before_kickoff": 144, "end_hours_before_kickoff": 96},
                "MID": {"start_hours_before_kickoff": 60, "end_hours_before_kickoff": 36},
                "FINAL": {"start_hours_before_kickoff": 6, "end_hours_before_kickoff": 2},
            },
            kelly_fraction="0.20", standard_max_bankroll_fraction="0.20",
            exceptional_max_bankroll_fraction="0.30",
            minimum_stake_cents=25, stake_increment_cents=25, pounce_limit=1,
            attribution_confidence_threshold="0.700",
            effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ))
        return season.id


def _week(season_id, number=4):
    with session_scope() as session:
        return session.execute(
            select(Week).where(Week.season_id == season_id, Week.week_number == number)
        ).scalar_one_or_none()


def _count(model) -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(model)).scalar()


def test_a_prepared_week_is_pending_and_not_opened():
    season_id = _season("pending")
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)

    week = _week(season_id)
    assert week is not None
    assert week.status == "PENDING"
    assert week.opened_at is None


def test_preparing_emits_no_week_opened_event():
    """Opening a week is a competitive declaration. Creating the FK target
    a precommitted slate points at is not."""

    season_id = _season("no-event")
    before = _count(CompetitionEvent)
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    assert _count(CompetitionEvent) == before


def test_preparing_touches_nothing_else():
    from app.db.models.forecast_lab import BenchmarkSlatePlan
    from app.db.models.settlement import BankrollTransaction

    season_id = _season("narrow")
    before = (_count(Game), _count(BankrollTransaction), _count(BenchmarkSlatePlan))
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    assert (_count(Game), _count(BankrollTransaction), _count(BenchmarkSlatePlan)) == before


def test_preparation_is_idempotent():
    season_id = _season("idempotent")
    first = apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    second = apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    assert first.week_id == second.week_id
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1


def test_a_contradictory_profile_is_refused_not_adjusted():
    """These flags decide whether a week counts toward the season. Changing
    them under an already-committed slate would rewrite what was agreed."""

    season_id = _season("conflict")
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    with pytest.raises(WeekPreparationRefused, match="not adjusted in place"):
        apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.COMPETITIVE)
    week = _week(season_id)
    assert (week.is_real_money, week.counts_toward_standings, week.counts_toward_awards) \
        == (False, False, False)


def test_the_repository_refuses_a_conflicting_flag_on_its_own():
    """Defence in depth. The service checks first, so the repository's own
    guard is otherwise never exercised -- and the repository is what any
    future caller reaches."""

    from app.db.repositories.season_repository import (
        SeasonRepository,
        WeekConfigurationConflict,
    )

    season_id = _season("repo-guard")
    with session_scope() as session:
        SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
        )
    with pytest.raises(WeekConfigurationConflict, match="not adjusted in place"):
        with session_scope() as session:
            SeasonRepository(session).prepare_week(
                season_id=season_id, week_number=4, profile=WeekProfile.COMPETITIVE
            )
    assert _week(season_id).is_real_money is False


def test_idempotency_checks_all_three_flags_not_just_the_money():
    """Two profiles that AGREE about money and differ about standings must
    not be treated as the same week. Comparing only `is_real_money` would
    accept a half-rehearsal row as a match for a rehearsal request -- the
    exact confusion week profiles exist to remove."""

    from app.db.repositories.season_repository import (
        SeasonRepository,
        WeekConfigurationConflict,
    )

    season_id = _season("same-money-different-week")
    with session_scope() as session:
        # A half-rehearsal planted directly: no money, but it counts.
        session.add(Week(
            season_id=season_id, week_number=4, is_real_money=False,
            counts_toward_standings=True, counts_toward_awards=True,
            status="PENDING",
        ))

    with pytest.raises(WeekConfigurationConflict, match="not adjusted in place"):
        with session_scope() as session:
            SeasonRepository(session).prepare_week(
                season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
            )

    week = _week(season_id)
    assert (week.counts_toward_standings, week.counts_toward_awards) == (True, True), (
        "the conflicting row was adjusted rather than refused"
    )


def test_the_service_also_refuses_a_money_matching_profile_mismatch():
    season_id = _season("service-same-money")
    with session_scope() as session:
        session.add(Week(
            season_id=season_id, week_number=4, is_real_money=False,
            counts_toward_standings=True, counts_toward_awards=False,
            status="PENDING",
        ))
    with pytest.raises(WeekPreparationRefused, match="NONSTANDARD"):
        plan_preparation(
            season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
        )


def test_the_repository_returns_the_same_row_for_an_identical_request():
    from app.db.repositories.season_repository import SeasonRepository

    season_id = _season("repo-idempotent")
    with session_scope() as session:
        first = SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
        ).id
    with session_scope() as session:
        second = SeasonRepository(session).prepare_week(
            season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
        ).id
    assert first == second


def test_opening_transitions_a_prepared_week_rather_than_creating_one():
    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("transition")
    prepared = apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)

    opened_id = SeasonCommissioner(season_id=season_id).open_week(
        week_number=4, profile=WeekProfile.REHEARSAL
    )
    assert uuid.UUID(opened_id) == prepared.week_id, "opening created a second row"

    week = _week(season_id)
    assert week.status == "OPENED"
    assert week.opened_at is not None
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1


def test_opening_without_preparing_first_still_works():
    """The old single-call ergonomics survive: existing callers are
    unchanged while production gains the ability to prepare separately."""

    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("legacy")
    SeasonCommissioner(season_id=season_id).open_week(week_number=4, is_real_money=False)
    week = _week(season_id)
    assert week.status == "OPENED"
    assert week.opened_at is not None


def test_the_dry_run_writes_nothing(capsys):
    season_id = _season("dry")
    code = main([
        "--season-id", str(season_id), "--week-number", "4", "--mode", "rehearsal",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert "WOULD CREATE" in out
    assert "does NOT open the competition" in out
    assert _week(season_id) is None


def test_the_dry_run_reports_an_existing_week(capsys):
    season_id = _season("dry-existing")
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    main(["--season-id", str(season_id), "--week-number", "4", "--mode", "rehearsal"])
    out = capsys.readouterr().out
    assert "EXISTS ALREADY" in out
    assert "PENDING" in out


def test_week_zero_is_allowed_because_the_documents_define_it():
    """CONSTITUTION.md §6 and RULES.md §3 define a formal Week 0 at
    `week_number = 0`. Refusing it here would make the documented rehearsal
    week unpreparable. Registration keeps its own >= 1 guard, because a Game
    must belong to a real NFL week."""

    season_id = _season("week-zero")
    plan = plan_preparation(
        season_id=season_id, week_number=0, profile=WeekProfile.REHEARSAL
    )
    assert plan.week_number == 0


def test_a_negative_week_is_refused():
    season_id = _season("week-negative")
    with pytest.raises(WeekPreparationRefused, match="cannot be negative"):
        plan_preparation(
            season_id=season_id, week_number=-1, profile=WeekProfile.REHEARSAL
        )


def test_the_cli_refuses_cleanly(capsys):
    code = main([
        "--season-id", str(uuid.uuid4()), "--week-number", "4", "--mode", "rehearsal",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    assert "Traceback" not in out


def test_preparation_reaches_no_provider():
    import ast
    import inspect

    from app.services import prepare_week

    tree = ast.parse(inspect.getsource(prepare_week))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("fetch_schedule", "list_events", "fetch_quotes",
                      "TheOddsApiProvider", "commit_official_slate"):
        assert forbidden not in names, f"week preparation reaches {forbidden}"


# --- the profiles are read from the documents, not invented -------------


def test_the_two_profiles_match_the_governing_documents():
    """CONSTITUTION.md §6: Week 0 carries "no real wagers / no official
    bankroll results / no standings / no season awards". RULES.md §3 spells
    the same thing as three false flags. A competitive week is the
    complement."""

    assert WEEK_PROFILES[WeekProfile.REHEARSAL] == WeekFlags(False, False, False)
    assert WEEK_PROFILES[WeekProfile.COMPETITIVE] == WeekFlags(True, True, True)
    assert len(WEEK_PROFILES) == 2, (
        "a third profile appeared; modes are read from the documents, not "
        "invented, so a new one needs a documented requirement"
    )


def test_preparation_persists_all_three_flags():
    for mode, expected in (
        (WeekProfile.REHEARSAL, (False, False, False)),
        (WeekProfile.COMPETITIVE, (True, True, True)),
    ):
        season_id = _season(f"flags-{mode}")
        apply_preparation(season_id=season_id, week_number=4, profile=mode)
        week = _week(season_id)
        assert (
            week.is_real_money, week.counts_toward_standings, week.counts_toward_awards
        ) == expected, f"{mode} did not persist {expected}"


def test_a_half_rehearsal_is_nonstandard_not_a_rehearsal():
    """The exact state the old CLI could create: no money on it, still
    counting toward standings and awards."""

    assert profile_of(WeekFlags(False, True, True)) is None
    assert profile_of(WeekFlags(True, False, False)) is None


def test_the_cli_has_no_loose_boolean_interface():
    """--no-real-money used to leave standings and awards True. A mode
    selects a reviewed combination instead."""

    import pytest as _pytest

    # Introspected, not grepped out of the help text -- the help PROSE
    # deliberately mentions the old flag to explain why it is gone.
    with _pytest.raises(SystemExit):
        main(["--season-id", str(uuid.uuid4()), "--week-number", "4",
              "--no-real-money"])
    with _pytest.raises(SystemExit):
        main(["--season-id", str(uuid.uuid4()), "--week-number", "4",
              "--real-money"])
    with _pytest.raises(SystemExit):
        # A mode is required; there is no default that silently picks one.
        main(["--season-id", str(uuid.uuid4()), "--week-number", "4"])


# --- WEEK_OPENED fires exactly once ------------------------------------


def _week_opened(season_id) -> int:
    with session_scope() as session:
        return session.execute(
            select(func.count()).select_from(CompetitionEvent).where(
                CompetitionEvent.season_id == season_id,
                CompetitionEvent.event_type == "WEEK_OPENED",
            )
        ).scalar()


def test_the_first_open_emits_exactly_one_week_opened():
    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("open-once")
    SeasonCommissioner(season_id=season_id).open_week(
        week_number=4, profile=WeekProfile.REHEARSAL
    )
    assert _week_opened(season_id) == 1


def test_a_repeated_open_emits_no_second_event():
    """The repository was already idempotent, but the commissioner
    published on every call -- writing a second "the week opened" into the
    competition log for something that did not happen."""

    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("open-twice")
    commissioner = SeasonCommissioner(season_id=season_id)
    first = commissioner.open_week(week_number=4, profile=WeekProfile.REHEARSAL)
    second = commissioner.open_week(week_number=4, profile=WeekProfile.REHEARSAL)
    assert first == second
    assert _week_opened(season_id) == 1


def test_two_concurrent_opens_emit_exactly_one_event():
    """Serialized by a row lock, not by Python call ordering -- otherwise
    the duplicate event is a race rather than a bug."""

    import threading

    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("open-race")
    apply_preparation(season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL)
    start = threading.Barrier(2)
    errors: list[str] = []

    def worker():
        try:
            start.wait(timeout=10)
            SeasonCommissioner(season_id=season_id).open_week(
                week_number=4, profile=WeekProfile.REHEARSAL
            )
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    assert _week_opened(season_id) == 1, "two opens wrote two WEEK_OPENED events"
    assert _week(season_id).status == "OPENED"


def test_concurrent_opens_on_an_ABSENT_week_also_produce_one_event():
    """The earlier concurrency test prepared the week first, so it only
    proved the row lock works once a row EXISTS. `prepare_week`'s lookup is
    necessarily unlocked -- there is nothing to lock yet -- so two callers
    can both miss and both insert, and the unique index decides. The loser
    must recover into the winner's row, not surface an IntegrityError."""

    import threading

    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("open-race-absent")
    assert _week(season_id) is None, "the fixture prepared the week; nothing is proved"

    start = threading.Barrier(6)
    errors: list[str] = []

    def worker():
        try:
            start.wait(timeout=10)
            SeasonCommissioner(season_id=season_id).open_week(
                week_number=4, profile=WeekProfile.REHEARSAL
            )
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1
    assert _week_opened(season_id) == 1
    assert _week(season_id).status == "OPENED"


def test_week_creation_serializes_on_the_season_row():
    """DETERMINISTIC, not thread-luck. One session takes the Season lock
    and holds it; a second preparation must BLOCK until it is released.

    The existence check has no week row to lock yet, so without this the
    two callers both miss and both insert. Locking the durable parent
    removes the race rather than recovering from a unique violation."""

    import threading
    import time

    from sqlalchemy import select as sa_select

    from app.db.models.season import Season
    from app.db.repositories.season_repository import SeasonRepository

    season_id = _season("serialize")
    holder_ready = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def holder():
        with session_scope() as session:
            session.execute(
                sa_select(Season).where(Season.id == season_id).with_for_update()
            ).scalar_one()
            holder_ready.set()
            release.wait(timeout=20)

    def preparer():
        with session_scope() as session:
            SeasonRepository(session).prepare_week(
                season_id=season_id, week_number=4, profile=WeekProfile.REHEARSAL
            )
        finished.set()

    h = threading.Thread(target=holder)
    h.start()
    assert holder_ready.wait(timeout=10), "the holder never took the lock"

    pthread = threading.Thread(target=preparer)
    pthread.start()
    time.sleep(1.0)
    assert not finished.is_set(), (
        "preparation completed while the Season row was locked -- it is not "
        "serializing on the parent"
    )

    release.set()
    h.join(timeout=20)
    assert finished.wait(timeout=20), "preparation never completed after release"
    pthread.join(timeout=20)
    assert _week(season_id) is not None


def test_concurrent_identical_preparations_converge_on_one_row():
    for mode in (WeekProfile.REHEARSAL, WeekProfile.COMPETITIVE):
        season_id = _season(f"same-{mode}")
        ids = _prepare_concurrently(season_id, [mode] * 6)
        assert ids["errors"] == [], ids["errors"]
        assert len(set(ids["week_ids"])) == 1, "callers got different week rows"
        with session_scope() as session:
            assert session.execute(
                select(func.count()).select_from(Week).where(Week.season_id == season_id)
            ).scalar() == 1


def test_concurrent_conflicting_preparations_leave_one_reviewed_profile():
    """One coherent profile wins; the losers get a clean conflict. No
    interleaving may produce a NONSTANDARD combination."""

    from app.domain.week_profile import profile_of as _profile_of

    season_id = _season("conflicting")
    result = _prepare_concurrently(
        season_id,
        [WeekProfile.REHEARSAL, WeekProfile.COMPETITIVE] * 3,
    )
    assert all("WeekConfigurationConflict" in e for e in result["errors"]), result["errors"]
    assert result["errors"], "no caller was refused; both profiles cannot both win"

    week = _week(season_id)
    flags = WeekFlags(
        week.is_real_money, week.counts_toward_standings, week.counts_toward_awards
    )
    assert _profile_of(flags) is not None, (
        f"a concurrent race produced a NONSTANDARD week: {flags.describe()}"
    )
    with session_scope() as session:
        assert session.execute(
            select(func.count()).select_from(Week).where(Week.season_id == season_id)
        ).scalar() == 1


def _prepare_concurrently(season_id, profiles):
    import threading

    from app.db.repositories.season_repository import SeasonRepository

    start = threading.Barrier(len(profiles))
    week_ids: list[uuid.UUID] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(profile):
        try:
            start.wait(timeout=15)
            with session_scope() as session:
                row = SeasonRepository(session).prepare_week(
                    season_id=season_id, week_number=4, profile=profile
                )
                got = row.id
            with lock:
                week_ids.append(got)
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(p,)) for p in profiles]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return {"week_ids": week_ids, "errors": errors}


def test_preparation_locks_the_parent_before_checking_existence():
    """Order is the whole point: checking first and locking after would
    leave exactly the window this closes."""

    import ast
    import inspect
    import textwrap

    from app.db.repositories.season_repository import SeasonRepository

    source = textwrap.dedent(inspect.getsource(SeasonRepository.prepare_week))
    tree = ast.parse(source)
    locked_line = next(
        (n.lineno for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "with_for_update"),
        None,
    )
    assert locked_line is not None, "prepare_week does not lock anything"
    assert "SeasonRow" in source, "prepare_week no longer locks the durable parent"

    week_lookup = source.index("WeekRow.week_number == week_number")
    lock_at = source.index("with_for_update")
    assert lock_at < week_lookup, (
        "the existence check happens BEFORE the lock, which leaves the race open"
    )


def test_preparation_does_not_rely_on_catching_a_unique_violation():
    """The unique index stays the hard backstop, but the guarantee comes
    from serialization -- not from recovering after a collision."""

    import ast
    import inspect
    import textwrap

    from app.db.repositories.season_repository import SeasonRepository

    # The DOCSTRING names IntegrityError -- it explains the race this
    # replaced. Strip it, or this asserts against its own prose.
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(SeasonRepository.prepare_week))
    )
    function = tree.body[0]
    if (function.body and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)):
        function.body = function.body[1:]
    code = ast.unparse(function)

    assert "IntegrityError" not in code, (
        "prepare_week recovers from a unique violation instead of "
        "serializing to prevent it"
    )
    assert "begin_nested" not in code


def test_a_closed_week_never_reopens():
    from app.db.repositories.season_repository import (
        SeasonRepository,
        WeekConfigurationConflict,
    )
    from app.services.season_commissioner import SeasonCommissioner

    season_id = _season("closed")
    week_id = uuid.UUID(
        SeasonCommissioner(season_id=season_id).open_week(
            week_number=4, profile=WeekProfile.REHEARSAL
        )
    )
    with session_scope() as session:
        SeasonRepository(session).close_week(week_id, closed_at=NOW)

    with pytest.raises(WeekConfigurationConflict, match="not reopened"):
        SeasonCommissioner(season_id=season_id).open_week(
            week_number=4, profile=WeekProfile.REHEARSAL
        )
    assert _week(season_id).status == "CLOSED"
    assert _week_opened(season_id) == 1


def test_opening_locks_the_row_rather_than_trusting_call_order():
    import ast
    import inspect
    import textwrap

    from app.db.repositories.season_repository import SeasonRepository

    tree = ast.parse(textwrap.dedent(inspect.getsource(SeasonRepository.open_week)))
    locked = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "with_for_update"
    ]
    assert locked, "open_week reads the week without locking it"
