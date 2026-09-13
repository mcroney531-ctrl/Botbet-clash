# Backend — Phase 1 domain + Phase 2 persistence & Forecast Lab core

Implements constitution §117 **Phase 1** (`Season`, `SeasonRules`, `Week`,
`Competitor`, the bankroll ledger, `Ticket`, `Wager`, `Settlement`,
`CompetitionEvent` as plain Python domain objects), plus **Phase 2** split
into two parts:

- **2A — persistence**: a PostgreSQL/SQLAlchemy layer underneath the
  Phase 1 rules. The domain dataclasses in `app/domain/` are unchanged and
  untouched by this work; `app/db/` maps them to and from ORM rows
  explicitly, and `app/services/season_commissioner.py` is the
  persistence-backed equivalent of `app/domain/season_service.SeasonService`
  — same rules, same exceptions, same pure risk math, backed by real
  transactions instead of in-memory dicts.
- **2B — Forecast Lab core**: market math (de-vig, consensus), market/
  evidence snapshots, kickoff-relative checkpoint capture, the
  precommitted benchmark slate, `ForecastObservation` (benchmark +
  open-market, revisions as inserts), research settlement, and Brier/
  log-loss scoring — all under `app/forecast_lab/`.

No real AI provider or sports-data provider is wired in anywhere yet
(constitution Phases 3 and 6) — Phase 2B runs entirely against
caller-supplied fixture data. See `../RULES.md`, `../ARCHITECTURE.md`, and
`../DATABASE.md` for the rules and design this code implements.

## Layout

```
app/
  core/          money (integer cents), clock, id generation
  domain/        Phase 1: enums, models, ledger, risk math, event bus,
                 in-memory SeasonService — pure Python, no persistence
  db/
    base.py        declarative Base, naming convention, tz-aware datetime default
    session.py      engine/sessionmaker, session_scope() (one commit per unit of work)
    models/         ORM rows — a separate set of classes from app/domain/models.py
    mappers/        pure domain-dataclass <-> ORM-row translation, no logic
    repositories/   thin CRUD/query per aggregate, always season-scoped
  services/
    season_commissioner.py   persistent equivalent of domain.SeasonService
  forecast_lab/
    market_math.py             de-vig / consensus — pure functions
    market_snapshot_service.py canonical + consensus MarketSnapshot construction
    evidence_service.py        immutable EvidenceSnapshot creation
    checkpoint_service.py      idempotent, per-game OPENING/MID/FINAL capture
    benchmark_slate_service.py precommitted plan + per-game slot resolution
    forecast_service.py        ForecastObservation create/revise (insert-only)
    research_settlement_service.py  stat-value-only settlement + derived outcome
    scoring.py                  Brier / log-loss
    cohort.py                   common-coverage cohort + coverage %
  tests/
    test_*.py             Phase 1 domain + Phase 2B pure-math unit tests (no DB)
    integration/           Phase 2A/2B tests against real PostgreSQL
alembic/                  migrations (one so far: the structural core)
```

## Running the tests

Pure-domain and pure-math tests need no database:

```
cd backend
pip install -e ".[dev]"
pytest -q -m "not integration"
```

Integration tests need a running PostgreSQL reachable at `DATABASE_URL`
(defaults to `postgresql+psycopg://postgres:postgres@localhost:5432/botbet_test`):

```
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/botbet_test
alembic upgrade head
pytest -q
```

## Phase 2 readout

**Tables / migrations added** — one migration (`alembic/versions/*_structural_core.py`)
covering all 25 tables in `DATABASE.md`'s structural core: `seasons`,
`season_rules`, `weeks`, `competitors`, `season_competitors`, `games`,
`players`, `prop_markets`, `prop_quotes`, `market_snapshots`,
`checkpoint_runs`, `evidence_snapshots`, `forecast_observations`,
`benchmark_slate_plans`, `benchmark_slots`, `agent_sessions`,
`agent_session_evidence_snapshots`, `stake_recommendations`, `tickets`,
`wagers`, `pass_decisions`, `settlements`, `research_settlements`,
`bankroll_transactions`, `competition_events`. Show-layer-only tables
(`call_your_shots`, `weekly_receipts`, `awards`, `trash_talk_messages`,
`attribution_reviews`) are deferred, per the Phase 2 brief. Every
`UNIQUE`/`CHECK` constraint named in the brief is present and DB-enforced,
not just checked in Python — including a defense-in-depth test that
`settlements.wager_id UNIQUE` rejects a duplicate at the database level
even if the application-level `DuplicateSettlement` check is bypassed.
The hardening pass below added further `CHECK` constraints
(probability/confidence bounds on `forecast_observations`/
`market_snapshots`); since no real data exists yet in this dev/test
environment, that migration was regenerated in place rather than layered
as a second revision — a real deployment would add it as a new
migration instead.

**Repository / service boundaries** — `app/domain/` (Phase 1) has zero
imports from `app/db/` or `app/services/`; the dependency runs one way.
`app/db/models/` are separate ORM classes, never the domain dataclasses
reused as `Base` subclasses. `app/db/mappers/` do field-by-field
translation only — no validation, no business logic. `app/db/repositories/`
are thin CRUD/query wrappers around one `Session`, each scoped so a query
can't accidentally cross season boundaries (every competitive query goes
through `season_competitor_id`, never a bare provider string).
`app/services/season_commissioner.py` is where the actual rules live for
persistence — it reuses `app/domain/risk.py` and `app/domain/lines.py`
unchanged, and raises the identical `app/domain/errors.py` exceptions
Phase 1 already had tests for. Each public method is exactly one
`session_scope()` — one commit, or a full rollback — so "execute wager"
(ticket status + STAKE transaction + wager row) and "settle wager"
(settlement + return transaction + bankruptcy re-check) each land
atomically, never partially.

**Hardening pass (post-review)** — four integrity gaps found once a real
Commissioner and database existed to enforce them against:
- **Cross-season references were not rejected.** `SeasonCommissioner`
  now validates every week/competitor/market id against `self.season_id`
  before applying any rule to it (`_require_week_in_season`,
  `_require_competitor_in_season`, `_require_market_in_season`,
  raising the new `CrossSeasonReference`) — including validating a
  ticket's own week on `record_execution`, so a ticket id from a foreign
  season's Commissioner is rejected too, not just raw ids passed
  directly. Proven with adversarial tests that mix Season 1/Season 2 ids
  and confirm the transaction writes nothing.
- **The week lifecycle wasn't enforced.** `issue_ticket` and
  `record_pass` now require `week.status == "OPENED"` (`WeekNotOpen`
  otherwise), and `close_week` now requires every game scheduled for
  that week to be `FINAL` before it will close (a week with no games
  attached has nothing to block on).
- **No concurrency protection.** Two simultaneous requests could both
  observe "no official decision yet" (or "zero active Pounces") under
  READ COMMITTED and both proceed — there was no DB-level exclusion on
  either check. Added `_lock_competitor_week`, a
  `pg_advisory_xact_lock` keyed on `(season_competitor_id, week_id)`
  acquired at the start of `issue_ticket`, `record_pass`, and
  `record_execution`'s `PLACED` path; it auto-releases at
  commit/rollback, matching this class's one-transaction-per-method
  shape. **Verified the fix actually does something**: temporarily
  reverting the lock made the new race tests fail deterministically on
  every run (not flaky — every run), confirming the race is real and
  the lock is what closes it. A dedicated `weekly_decisions` table with
  `UNIQUE(season_competitor_id, week_id)` would be the cleaner
  long-term primitive; this is the documented V1 stopgap.
- **An expired Pounce still counted as active.**
  `active_pounce_tickets` checked `status == ISSUED` but never
  `valid_until`, so an expired-but-unretired Pounce could block a
  legal replacement (RULES.md §74). Fixed at the query level.

Two research-data integrity items landed alongside these: `CHECK`
constraints on `forecast_observations`/`market_snapshots` enforcing
`0 ≤ probability ≤ 1` and `1 ≤ confidence ≤ 10` at the database level
(not just in Python), and a cohort-integrity check — if two competitors'
"standardized" observations for the same checkpoint somehow point at
different evidence snapshots (which atomic checkpoint capture is
supposed to make impossible), the cohort now excludes that row with
`OTHER` rather than silently scoring it as a fair comparison.

**Declined from the same review round:** rejecting tickets on
Week 0 / non-real-money weeks. RULES.md §3 and constitution §6 are
explicit that Week 0 exists specifically to exercise the full weekly
lifecycle — including ticket issuance and "fake wager execution" —
through the *same* code paths as a real week, distinguished only by
`is_real_money=False` for downstream settlement/standings treatment.
Blocking ticket issuance on that flag would defeat Week 0's purpose
rather than harden anything; flagging this back rather than
implementing it silently.

**Tests added** — 74 total (was 45 at the end of Phase 1):
- 12 pure unit tests (no DB): `test_market_math.py` (de-vig, consensus,
  quantization) plus a `Money.floor_to_increment` regression already
  covered under Phase 1's suite.
- 4 Phase 2A integration tests (`test_season_commissioner.py`): the full
  create-season → register-competitors → open-week → issue-ticket →
  execute → **reload as a brand-new object** → settle → reject-duplicate
  → events-persisted → **Season 2 doesn't see Season 1's money** flow;
  an atomic-commit check; and the DB-level duplicate-settlement
  constraint test.
- 1 Phase 2B integration test (`test_forecast_lab_mocked_week.py`): the
  full 20-step mocked research week — see below.
- 10 hardening tests (`test_commissioner_integrity.py`): cross-season
  rejection (week/competitor/market/ticket), week-not-opened rejection
  (issue + pass), close-week games-not-final rejection and success, and
  Pounce expiry (expired doesn't block, unexpired still does).
- 2 concurrency tests (`test_concurrency.py`, 10 racing iterations each):
  simultaneous placements never both succeed, simultaneous Pounces never
  both succeed — both independently confirmed to fail reliably with the
  lock removed.
- 5 forecast-data integrity tests (`test_forecast_lab_constraints.py`):
  out-of-range probability/confidence rejected by the database, and the
  cohort evidence-snapshot-mismatch check (both the failure and the
  matching-snapshot success case).

**Mock week result** — `test_forecast_lab_mocked_week.py` passes end to
end: three games with independent kickoffs; a benchmark slate plan
committed from schedule alone; one game's OPENING checkpoint captured
(idempotently — a second call is a verified no-op) while a later game's
stays `PENDING`; its slots resolved (one to the eligible market, one
`UNFILLABLE` because its game's only market was pushable, with no
fallback candidate — reported, not backfilled); a market that fails
canonical-market eligibility entirely (no canonical quote) correctly
excluded; synthetic GPT/Claude/Gemini forecasts persisted against one
shared evidence snapshot; a MID capture with a moved line and a revision
row (parent unchanged, new row, new line); a single locked final stat
(228) that resolves the Opening line (225.5) **OVER** and the Mid line
(230.5) **UNDER** — the exact scenario the research-settlement redesign
exists for; Brier/log-loss computed for all three models plus the
canonical market and same-line consensus; a common-coverage cohort report
that correctly excludes the canonical-unavailable and pushable markets
(1/3 eligible, 33.3% coverage) with the right reason on each; and then
every score and the cohort report **recomputed from a fresh session with
nothing cached**, asserted bit-for-bit identical to the first pass.

**Known Week 0 decisions still intentionally unresolved** (per RULES.md
§19 and ARCHITECTURE.md §4a — these are configuration to freeze
empirically, not open design questions):
- `batch_methodology` (BATCH_10 / BATCH_SMALL / ISOLATED)
- exact checkpoint windows (currently the RULES.md-proposed 144–96h /
  60–36h / 6–2h, unvalidated against real prop availability)
- the benchmark-slate allocator's game-ordering rule (currently
  round-robin-by-kickoff, flagged in ARCHITECTURE.md §4a as having a
  real early-game bias that a schedule-ID-seeded shuffle should replace)
- the deterministic tiebreak used to pick among multiple eligible markets
  of the same stat type for one benchmark slot (currently lowest
  `player_id`, arbitrary but stable — needs a real rule)
- `kelly_fraction`, `weekly_decision_deadline`, notification channel(s)
  (unchanged from Phase 1 — no Phase 2 work touched these)

Not started (by design — constitution Phases 3, 6, 7+): real OpenAI/
Anthropic/Gemini calls, real odds/stats/news/weather providers, the
weekly orchestration state machine wiring Forecast Lab checkpoints to
Competition decisions end-to-end, attribution, the Show layer, and any
FastAPI routes beyond what exists (none yet — no HTTP surface was needed
to prove this phase).
