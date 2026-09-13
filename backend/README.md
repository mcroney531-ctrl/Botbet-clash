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

**Tests added** — 57 total (was 45 at the end of Phase 1):
- 12 new pure unit tests (no DB): `test_market_math.py` (de-vig, consensus,
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
