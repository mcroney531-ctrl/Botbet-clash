# Backend — Phase 1 domain, Phase 2 persistence & Forecast Lab core, Phase 3 AI adapter layer, Phase 4A real market + roster ingestion

Implements constitution §117 **Phase 1** (`Season`, `SeasonRules`, `Week`,
`Competitor`, the bankroll ledger, `Ticket`, `Wager`, `Settlement`,
`CompetitionEvent` as plain Python domain objects), **Phase 2** split
into two parts, and **Phase 3**'s real model adapter layer:

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
- **3 — AI adapter layer**: a provider-neutral `AIOrchestrator` that sends
  identical `BENCHMARK_FORECASTING` requests to OpenAI, Anthropic, and
  Gemini (plus a deterministic `MockAdapter` for tests), validates every
  response, and permanently records exactly what each model saw and
  returned via `AgentSession` + `ForecastObservation` — all under
  `app/ai/`. The model layer is an adapter, not the competition engine:
  it never touches bankroll, wagers, or settlement.

No real odds/stats/news/weather provider is wired in anywhere yet
(constitution Phase 6), and no autonomous wager placement exists — Phase 3
only proves the model layer can be trusted to observe and report, nothing
more. See `../RULES.md`, `../ARCHITECTURE.md`, and `../DATABASE.md` for the
rules and design this code implements.

This branch develops backend-only; a separate, independent workstream
("Astra") owns `frontend/` (Next.js/React Three Fiber arena UI) and is not
touched here — no `frontend/` directory exists on this branch as of Phase 3.

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
  ai/
    schemas/       pydantic request/response models (BENCHMARK_FORECASTING v1)
    prompts/       neutral prompt template + frozen prompt/schema versions
    providers/     base.py (Protocol/ProviderCallResult/ProviderError),
                   mock.py, openai.py, anthropic.py, gemini.py
    validation.py       provider-neutral response validation + quantization
    session_service.py  AgentSession persistence + audit receipts
    registry.py         provider string -> adapter factory (mock_registry / live_registry)
    orchestrator.py      AIOrchestrator: the only caller of a provider adapter
  tests/
    test_*.py             Phase 1/2B pure-math + Phase 3 pure schema/validation tests (no DB)
    integration/           Phase 2A/2B/3 tests against real PostgreSQL
                            (test_live_providers.py additionally needs real API keys)
alembic/                  migrations — one per phase's additive schema change
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

A `live_provider`-marked subset of the integration tests additionally
needs real credentials (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY` or `GOOGLE_API_KEY`) and makes real, billable calls to
each provider; they skip individually per missing key and never run in
routine CI. Everything else — including the entire mock-path AI
orchestrator flow — requires no credentials at all.

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

**Hardening pass 2 (relational-integrity round)** — three more gaps
found by mixing valid-looking ids across weeks/markets rather than
across seasons:
- **`issue_ticket` didn't check the terminal decision state.** It
  acquired the competitor/week lock but never called
  `_has_weekly_decision`, so a new ticket could be issued after
  `BET_EXECUTED` or `PASS_LOCKED` already applied — it would eventually
  fail at `record_execution`, but in the meantime the derived state held
  a terminal decision *and* a pending `ISSUED` ticket simultaneously,
  contradicting the two-terminal-states model (ARCHITECTURE.md §4 Axis
  2). Added the same `_has_weekly_decision` check `record_pass` and
  `record_execution` already had, right after the advisory lock.
- **Season membership wasn't the same as week membership.**
  `_require_market_in_week` (renamed from `_require_market_in_season`)
  now also checks `game.week_number == week.week_number`, raising the
  new `MarketNotInWeek` — a Week 1 ticket against a Week 12 prop in the
  same season used to pass the season-only check. Applied to both
  `issue_ticket`'s market and `record_pass`'s optional
  `best_available_candidate_market_id`, which wasn't validated at all
  before.
- **`create_forecast_observation` trusted two independently-supplied
  arguments that were supposed to agree.** It accepted `evidence_snapshot_id`
  and a `MarketSnapshot` object separately, so a caller bug could hand
  three competitors the same `evidence_snapshot_id` while stamping each
  observation's canonical line/prices from a *different* market
  snapshot — invisible to the cohort's shared-evidence check (§forecast
  cohort), which only ever compares the id, not what it points to. Fixed
  by removing the `market_snapshot` parameter entirely: the function now
  loads the `EvidenceSnapshot` by id and derives the market snapshot
  from `evidence.market_snapshot_id` itself, and rejects outright if
  `evidence.market_id != market_id`. The mismatch is now structurally
  impossible rather than merely checked for.

Also documented (no code change — explicitly deferred, doesn't block the
model-adapter phase): `close_week`'s "every game is FINAL" check
verifies *games are complete*, not that *research settlement is locked*.
Those are different signals — `Game.status == FINAL` just means the
football game ended; RULES.md §82's T+72h research lock
(`research_settlements` / `weeks.research_locked_at`) is separate and
unrelated. The eventual weekly orchestrator should split this into a
real `GAMES_COMPLETE → SETTLED → CLOSED` sequence gated on the research
lock, not on `Game.status` alone; `close_week`'s docstring now says this
explicitly rather than implying `FINAL` is the finalization signal.

**Tests added** — 80 total (was 45 at the end of Phase 1):
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
- 16 hardening tests (`test_commissioner_integrity.py`): cross-season
  rejection (week/competitor/market/ticket), week-not-opened rejection
  (issue + pass), close-week games-not-final rejection and success,
  Pounce expiry (expired doesn't block, unexpired still does),
  market-not-in-week rejection (ticket market and pass candidate, plus
  the same-week acceptance case), and a new ticket being rejected after
  either terminal decision (`BET_EXECUTED` or `PASS_LOCKED`).
- 2 concurrency tests (`test_concurrency.py`, 10 racing iterations each):
  simultaneous placements never both succeed, simultaneous Pounces never
  both succeed — both independently confirmed to fail reliably with the
  lock removed.
- 6 forecast-data integrity tests (`test_forecast_lab_constraints.py`):
  out-of-range probability/confidence rejected by the database, the
  cohort evidence-snapshot-mismatch check (both the failure and the
  matching-snapshot success case), and `create_forecast_observation`
  rejecting an evidence snapshot that belongs to a different market.

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

## Phase 3 readout

**Central question this phase answers**: can BotBet Clash reliably send
standardized forecasting tasks to three independent model providers and
permanently record exactly what each model saw and returned? Yes — proven
against MockAdapter end to end, and wired (not yet live-verified in this
environment; no provider API keys are configured here) against real
OpenAI/Anthropic/Gemini SDKs.

**Files/modules added** — all new, nothing in `app/domain/`,
`app/forecast_lab/`, or `app/services/` changed except the one additive
`AgentSession` migration below:
- `app/ai/schemas/common.py`, `benchmark_forecast.py` — pydantic v2
  `BenchmarkForecastRequest`/`MarketInput`/`MarketContext`,
  `BenchmarkForecastResponse`/`ForecastItem`, and
  `benchmark_response_json_schema()` (a hand-written plain JSON Schema —
  not derived from the pydantic models, whose `Decimal` fields would
  otherwise surface as schema type `"string"` — used by all three real
  adapters to constrain provider output).
- `app/ai/prompts/versions.py` (`benchmark-v2` / `forecast-v1`),
  `benchmark_forecasting.py` (the neutral system instruction, verbatim
  from the brief, plus `render_benchmark_prompt()` which returns the
  exact dict persisted as `AgentSession.rendered_request` — the rendered
  request is stored directly rather than reconstructed later from
  template + inputs, per the brief's "simpler and safer" guidance).
- `app/ai/providers/base.py` — `CompetitorAdapter` Protocol,
  `ProviderCallResult`/`ProviderError` dataclasses, the 8 normalized
  `ErrorCategory` values, `TRANSPORT_ERROR_CATEGORIES` (the set that
  means "no usable answer at all," as opposed to a validation-retryable
  response), and a shared `error_result()` helper.
- `app/ai/providers/mock.py` — deterministic `MockAdapter` + `MockOutcome`
  scripting every failure simulation the brief lists.
- `app/ai/providers/openai.py`, `anthropic.py`, `gemini.py` — real
  adapters (Phase 3B), each importing only its own provider's SDK; no
  provider SDK type crosses into `app/ai/orchestrator.py`,
  `app/forecast_lab/`, or `app/domain/`.
- `app/ai/validation.py` — provider-neutral content validation (pydantic
  shape + market coverage: no missing/unknown/duplicate `market_id`) and
  the single quantization policy (`PROBABILITY_PLACES` from
  `market_math.py` for `probability_over`; a new `CONFIDENCE_PLACES =
  Decimal("0.01")` matching `ForecastObservation.confidence`'s
  `NUMERIC(4,2)`).
- `app/ai/session_service.py` — `AgentSessionRepository` (PENDING →
  CALLING → VALID/INVALID/FAILED persistence, `agent_session_evidence_snapshots`
  as the authoritative multi-market input record), `build_orchestration_key()`,
  and `build_audit_receipt()`.
- `app/ai/registry.py` — `ProviderRegistry.for_competitor()` resolves an
  adapter from `Competitor.provider` + `SeasonCompetitor.model_identifier`
  only, never a display name; `mock_registry()` and `live_registry()` are
  the only two places that actually name a provider string, so swapping
  mock for real adapters never touches the orchestrator.
- `app/ai/orchestrator.py` — `AIOrchestrator`, scoped to one `season_id`
  like `SeasonCommissioner`. `run_benchmark_forecast()` and
  `run_benchmark_round()` are the only two public entry points.

**Database migration** — one additive migration,
`a8d1acc96058_agent_session_lifecycle.py` (Phase 2 is closed, so this is
a real `alembic revision --autogenerate` layered on top, not a
regenerate-in-place). Adds to `agent_sessions`: `status` (CHECK-constrained
to `PENDING`/`CALLING`/`VALID`/`INVALID`/`FAILED`), `orchestration_key`
(`UNIQUE`, the idempotency key), `rendered_request` (JSONB),
`provider_request_id`, `usage_metadata` (JSONB), `error_category`
(CHECK-constrained to the 8 normalized categories), `error_message`,
`transport_retry_count`, `correction_retry_count`, `completed_at`; makes
`raw_response`/`is_valid` nullable (unknown until the call returns).
`retry_count` (Phase 2's original column) is kept for back-compat and set
to `transport_retry_count + correction_retry_count` on every terminal
write — nothing reads it as the source of truth anymore.

**Provider-neutral interface** — `CompetitorAdapter.forecast_benchmark(request)
-> ProviderCallResult`. Zero provider conditionals exist in
`orchestrator.py`, `validation.py`, or `session_service.py`; every branch
that knows "which provider" lives inside that provider's own adapter
module or inside `registry.py`'s two factory functions.

**Provider adapters implemented**: OpenAI (Chat Completions +
`response_format: json_schema`, `strict: true`), Anthropic (Claude's
`output_config.format: json_schema`), Gemini (`response_mime_type:
application/json` + `response_json_schema`) — one official SDK per
provider (`openai`, `anthropic`, `google-genai`), each reading its own
credential env var (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`/`GOOGLE_API_KEY`) via the SDK's own default behavior;
no key is ever read, logged, or handled by adapter code directly.
Exceptions from each SDK are mapped to the 8 normalized categories
(auth → `AUTHENTICATION_ERROR`, rate limit → `RATE_LIMITED`, timeout →
`TIMEOUT`, connection/5xx → `PROVIDER_UNAVAILABLE`, a
safety/refusal finish reason → `CONTENT_REFUSAL`, unparseable/empty
content → `INVALID_PROVIDER_RESPONSE`, anything else → `UNKNOWN_PROVIDER_ERROR`).

**Prompt/schema versions**: `prompt_version = "benchmark-v2"`,
`schema_version = "forecast-v1"`. (v2 moved the output-field semantics —
confidence's 1–10 conviction scale, `key_factors`' five-item cap,
`probability_over`'s 0–1 range — out of the request JSON Schema and into
the instruction text, after Anthropic's structured-outputs dialect turned
out to reject `minimum`/`maximum`/`maxItems`. Found live: with those
keywords gone and nothing else carrying the meaning, all three providers
independently returned `confidence` as a 0–1 value. The response schema
itself did not change, so `schema_version` stayed at v1.)
Only `BENCHMARK_FORECASTING` exists;
`OPEN_MARKET_RESEARCH`/`STAKE_SIZING`/`FINAL_DECISION`/etc. are not
implemented. The system instruction is neutral and identical for all
three competitors — no personality, no "be conservative"/"be aggressive"
per-provider text anywhere.

**Validation behavior**: pydantic shape (probability/confidence ranges,
`uncertainty` enum, non-blank text, `key_factors` ≤ 5, `extra="forbid"`
on every model) first, then market coverage (no missing, no unknown, no
duplicate `market_id` — checked against the exact request, not just
"is this valid JSON"), then quantization. A partial match (e.g. 2 of 3
requested markets present) is rejected outright, not silently accepted
as a smaller success.

**Retry/correction behavior**: transport failures (no usable
payload — timeout, auth, provider-unavailable, unknown) are terminal
immediately and recorded as `FAILED`; they are tracked via
`transport_retry_count`, which stays `0` in Phase 3A/3B since no
automatic transport-level retry loop is implemented yet (a deliberate,
documented scope boundary — the field exists so that behavior can be
added later without another migration). Schema-invalid responses get
exactly one bounded correction retry (`max_correction_retries=1` by
default) using the same request; still-invalid after that retry is
recorded as `INVALID` (not `FAILED` — the provider *did* respond).
`transport_retry_count` and `correction_retry_count` are tracked as
distinct columns, never collapsed into one generic counter, so "timed
out three times" and "answered twice but failed validation twice" are
never confused when auditing a session later.

**AgentSession lifecycle**: `PENDING` (created and committed *before* any
provider is contacted, together with its `agent_session_evidence_snapshots`
input rows) → `CALLING` (committed in its own transaction just before the
external call) → `VALID`/`INVALID`/`FAILED`. No DB transaction is ever
held open across a network call. Idempotency: `orchestration_key =
f"{season_competitor_id}:{checkpoint_run_id}:BENCHMARK_FORECASTING:benchmark-v2"`,
`UNIQUE`-constrained; a rerun that already produced a `VALID` session for
that key returns the existing session id without ever resolving an
adapter or touching a provider — proven with a "poison" registry in the
integration test that raises if it's ever asked to resolve an adapter.

**MockAdapter failure simulations** (one test per simulation, all pure/no-DB
in `app/tests/test_ai_validation.py`, plus a full-orchestrator failure
round in the Postgres integration test): valid response; malformed JSON
(never reaches validation — it's a transport-shaped `INVALID_PROVIDER_RESPONSE`);
missing market; duplicate market; unrequested/unknown market; probability
> 1; invalid confidence; invalid uncertainty; provider timeout; provider
unavailable; valid result after one correction retry; still-invalid
result after the correction retry is exhausted (rejected, zero
`ForecastObservation`s created).

**Test count** (as of Phase 3): 102 passed, 4 skipped (the 4 `live_provider` tests, no
credentials configured in *this* environment — they are not the live
proof, see below) — up from 80 at the end of Phase 2. New: 18 pure tests
(`test_ai_validation.py`, `test_ai_live_smoke.py`), 3 mock-path
integration tests (`test_ai_orchestrator.py`, `test_live_smoke.py`,
Postgres), 4 live-provider integration tests (`test_live_providers.py`,
Postgres + real API keys, skipped here). Zero regressions in any
Phase 1/2 test.

**Mock-path acceptance result** (`test_ai_orchestrator.py`): one season,
three season-competitors (openai/anthropic/google), one captured OPENING
checkpoint's `EvidenceSnapshot` run through `run_benchmark_round()` for
all three — 3 distinct `AgentSession`s, all `VALID`, each pointing at the
same evidence snapshot via `agent_session_evidence_snapshots`, each with
its own provider/model_identifier/raw_response/validated_response, each
producing exactly one `ForecastObservation` with the fixture's exact
probability; a rerun against a poison registry proves idempotency without
ever calling an adapter; audit receipts reconstructed from a fresh
session match every field. A second game then exercises all three
non-happy paths simultaneously: one competitor succeeds after exactly one
correction retry, one is rejected after exhausting its correction retry
(`INVALID`, zero `ForecastObservation`s), and one fails immediately on a
simulated timeout (`FAILED`, `raw_response` is `None`, zero
`ForecastObservation`s) — and the first competitor's success is
unaffected by the other two's failures in the same round.

**Real provider path status: PASSED live (2026-09-16).** Run from inside
the deployed Railway container via
`railway ssh --service Botbet-clash --environment production -- /opt/venv/bin/python -m app.ai.live_smoke`
(see `app/ai/live_smoke.py` — a production-safe CLI, not pytest, since
pytest is a dev-only dependency and a public endpoint firing billable
provider calls would be its own bad idea):

```
OPENAI     VALID   session=b8d256a2-…   p_over=.52000
ANTHROPIC  VALID   session=573e9fc1-…   p_over=.51000
GOOGLE     VALID   session=1fb599f9-…   p_over=.50800

shared_evidence=true          three_valid_sessions=true
three_forecast_observations=true    audit_receipts_valid=true
rendered_payload_equivalent=true    no_response_leakage=true
probabilities_in_range=true

PHASE 3 LIVE PROVIDER PROOF: PASS
```

Three real providers, one identical frozen `EvidenceSnapshot`, three
independent `AgentSession`s, three `ForecastObservation`s, audit receipts
reconstructing from a fresh session. Four real bugs surfaced only by
running it for real, each fixed and pushed: `GeminiAdapter` let an
uncaught `httpx.ConnectError` escape and strand a session at `CALLING`
(orchestrator now has a catch-all backstop); Anthropic's
structured-outputs JSON Schema dialect rejects `minimum`/`maximum` on
number properties; `gemini-2.5-flash` had been retired out from under the
account; and once those schema keywords were removed, nothing carried the
1–10 confidence scale any more and all three providers returned it as a
0–1 value (→ prompt `benchmark-v2`).

The 4 `live_provider` pytest tests remain the local-development
equivalent and still skip without credentials; they are not what proved
this.

**Do not read anything into the forecast differences above.** `.52000`
/ `.51000` / `.50800` on a single synthetic market, all clustered near
the canonical de-vigged baseline, is a connectivity check — n=1, one
fabricated market, mocked evidence. It says nothing about calibration or
which model forecasts better. Model comparison is explicitly out of scope
for Phase 3.

**Known Week 0 decisions still intentionally unresolved** (unchanged from
Phase 2, plus the two Phase 3 added): `BATCH_10`/`BATCH_SMALL`/`ISOLATED`
benchmark batching (the schema already supports 1..N markets per call —
`AIOrchestrator._build_market_batch` takes an arbitrary list of
`evidence_snapshot_ids` — but the production choice isn't frozen), and the
exact production model roster/version per provider (the smoke proof uses
small/cheap models — `gpt-4o-mini`,
`claude-haiku-4-5`, `gemini-3.8-flash` — deliberately not
presented as the season's real roster). All the Phase 2 items
(checkpoint window tuning, benchmark-slate game-order allocator, same-stat
tiebreak, `kelly_fraction`, weekly decision deadline) remain open too.

**Astra frontend coordination**: no `frontend/` directory exists on this
branch as of Phase 3 — nothing needed to be preserved or avoided. All
Phase 3 changes are confined to `backend/` (plus this README); no
production arena/HTTP endpoints were added, and canonical backend event
names (`POUNCE_ISSUED`, `BET_EXECUTED`, `PASS_DECLARED`, `PROP_WON`,
`PROP_LOST`, `BANKRUPTCY`, `BANKROLL_CHANGED`) are untouched.

Not started (by design — constitution Phases 4+, 6, 7+, and explicitly
out of Phase 3's scope per its kickoff brief): real odds/stats/news/
weather ingestion, automatic sportsbook execution, autonomous wager
placement, the weekly production scheduler, `OPEN_MARKET_RESEARCH`/
`STAKE_SIZING`/`FINAL_DECISION`/`POSTMORTEM`/`PUBLIC_COMMENTARY`/
`TRASH_TALK` prompt families, the Show layer, and Astra/arena
integration.


## Phase 4A.1 readout — real market-data ingestion seam

**Status: the provider boundary exists and is proven against a mock. No
real market data has been ingested, and none can be until the
provider-validation probe runs.** The authoritative design contract is
[`docs/phase4-ingestion-seam.md`](docs/phase4-ingestion-seam.md); where
this readout and that document disagree, the document wins.

### What shipped

`app/marketdata/` mirrors `app/ai/` deliberately, so the model-provider
and market-data boundaries read the same way:

    base.py             MarketDataProvider Protocol, ProviderFetchResult,
                        MarketDataError, ProviderDiagnostic
    dto.py              frozen normalized DTOs
    mapping.py          vendor market key -> StatFamily (tentative/verified split)
    registry.py         provider name -> adapter
    ingestion.py        IngestionService: the only writer of Game/Player/
                        PropMarket/PropQuote from external data
    telemetry.py        ingestion_runs / provider_calls, quota, sanitization
    provenance.py       synthetic provenance for non-provider quotes
    providers/
      the_odds_api.py   the ONLY module that may know vendor JSON exists
      mock.py           12 deterministic scenarios
    validation_probe.py the free-tier discovery CLI

Adapters never touch the database. `IngestionService` owns every mapping
into our persistence conventions, so swapping providers cannot change how
our rows are shaped.

### Two corrections to shipped Phase 2B code

**`quotes_as_of` now selects on observation time, not retrieval time.**
`PropQuote` had exactly one timestamp, `retrieved_at`, and quote selection
filtered and ordered on it. A backfill retrieved on 20 September for a
5 September snapshot would have been excluded from the 5 September
checkpoint and silently included in every checkpoint after the 20th,
presenting two-week-old prices as current market state. `as_of_at` is now
the market-state clock; `retrieved_at` remains as operational provenance.
The load-bearing comment in `market_snapshot_service.py` moved with it.

**Quote selection is source-pinned.** `SeasonRules.market_data_provider`
is frozen alongside `canonical_sportsbook`, and `quotes_as_of` requires a
source rather than accepting one optionally — so a second provider cannot
silently blend two feeds into one consensus, and a midseason vendor switch
cannot change the research baseline behind our backs.

`capture_checkpoint` was deliberately left alone. It is a live-window
operation that marks a run `MISSED` when `now` is past `window_end`, so it
cannot serve a backfill; historical reconstruction gets its own Phase 4B
entry point rather than weakening the live rules.

### Observations, not polls

Repeated unchanged observations are retained. Two polls four hours apart
are two rows even when nothing moved, because keeping only the first makes
"market genuinely unchanged and freshly observed" indistinguishable from
"our feed stopped seeing that book" — a checkpoint-freshness question.

Idempotency comes from provenance instead: `PropQuote.provider_call_id`
plus a fingerprint over `(provider_call_id, event, player, stat_family,
sportsbook, line, prices, parser_version)`. Reprocessing a stored response
reuses its original call id and is a no-op; a fresh poll is a new call id
and legitimately records another observation.

`parser_version` is stored and fingerprinted but **quote selection does
not filter on it**. A global "newest parser wins" filter would make every
week still parsed by v1 vanish the moment v2 deployed. The supersession
policy is deferred until parser replay actually exists.

### What is deliberately NOT implemented

`TheOddsApiProvider.fetch_quotes` raises `NotImplementedError`. This is
the point, not an omission. Five things about the vendor's player-prop
payload are unknown — the exact market keys, whether a stable player id
exists, whether team and position appear at all, whether alternate lines
are separate keys or extra lines in one key, and the level at which
`last_update` is reported — and each one changes what the parser should
be. Writing it against assumptions is what the seam exists to prevent.

The tentative/verified mapping split enforces this mechanically rather
than by convention: `VERIFIED_MARKET_KEYS` is empty, and asking for
verified keys raises `UnverifiedMarketMappingError`. Production cannot run
on an unobserved spelling even if someone forgets why.

All five market keys are now documented by the vendor, and alternates are
documented under separate `_alternate` keys — so the mapping needs no
alternate-handling logic, since unlisted keys are simply never mapped. But
documented is not observed: the gate tracks whether a live payload has
actually returned them, which is a different and stronger claim.

Payload inspection for the probe lives in the adapter as
`discover_event_shape()` and returns a neutral `ProviderShapeReport`. The
CLI never indexes into vendor JSON — including in diagnostic code, since
exempting diagnostics is how schema leakage starts. A unit test asserts the
probe module contains no vendor field names.

### Credential handling

The Odds API authenticates with an `apiKey` **query parameter**, which
makes the request URL itself a secret — and means the ordinary reflex of
surfacing `str(exc)` is a credential leak, since httpx embeds the full URL
in transport exception text. Every `except` in the adapter constructs its
own fixed message. `telemetry.sanitize_message` strips URLs and
credential-shaped assignments as a second line of defence, and regression
tests assert a sentinel key never reaches an exception message, a result
object, a persisted row, or probe output.

The backend boots and serves `/health` with `THE_ODDS_API_KEY` absent; the
key is required only when the adapter is actually invoked.

### Test count

**154 passed, 4 skipped** (up from 102/4 at the end of Phase 3, zero
regressions). The migration was verified against a populated database: two
byte-identical legacy quotes received distinct fingerprints via the
documented `sha256("legacy:" + id)` exception, which a content hash would
have collided.

### What must happen next

The probe runs **inside Railway** (`railway ssh`), on the free tier, at
roughly 5 credits, with research persistence off. It answers the nine
deliverables in `docs/phase4-ingestion-seam.md` §15 — most importantly
whether the provider supplies player team and position at all, since
`Player.team` and `Player.position` are `NOT NULL` and placeholder values
like `"UNKNOWN"` are prohibited outright. If it supplies neither,
production Week-0 ingestion is blocked pending a roster source or a schema
decision, and that is the correct outcome rather than a problem to code
around.

Phase 4 makes the **market** component real. It does not make recent player
stats, injuries, news, weather, or team context real — those remain mocked
Phase-2 payload content, and an `EvidenceSnapshot` must not be described as
"fully real" merely because its `MarketSnapshot` is.


## Phase 4A.2 readout — real market data, real player identity

**Status: CLOSED.** Real NFL market data and real player identity are
persisted end to end, proven by two live runs against a real game.

Two external boundaries, one internal model:

    The Odds API    -> what market exists, and at what price
    nflverse        -> who that human is, and which side of the game
    BotBet Clash    -> the canonical identity and team model in between

Neither provider defines our vocabulary. Design contracts:
[`docs/phase4-ingestion-seam.md`](docs/phase4-ingestion-seam.md) and
[`docs/phase4-roster-identity-seam.md`](docs/phase4-roster-identity-seam.md).

### What two live runs proved

One real game (`DET @ BUF`, week 3), sampled twice ~16 hours apart, the
second inside the `FINAL` checkpoint window. Verified afterwards with a
read-only inspector that spends no credits:

| | |
| --- | --- |
| `GamePlayer` rows | 16 |
| `GamePlayerObservation` rows | 30 (14 + 16) |
| per relationship | 14 with two observations, 2 with one |
| distinct roster provider calls | 2 |
| resolver versions | `two-team-exact-v1` (14), `two-team-exact-alias-v2` (16) |
| **relationships whose observations disagree with the accepted team** | **0** |
| `PropQuote` rows | 336 (164 + 172) |
| distinct market provider calls | 2 |
| distinct `as_of_at` observation times | 2 |

The revalidation contract holds on real data: the second run **appended**
thirty observations' worth of evidence without rewriting a single accepted
relationship.

### The retention rule, demonstrated both ways

Of 152 book/market pairs present at both observation times, **127 moved**
and **25 held identical**. Both cases produced two rows.

The 25 are the point. DraftKings held Josh Allen passing touchdowns at
1.50 / −165 / +129 across both runs — the identical 0.58777 de-vigged
probability — while the six-book consensus drifted 0.59362 → 0.60296.
Under state-change suppression we would have written nothing for
DraftKings the second time and lost the evidence that it was *still
quoting* at kickoff minus four hours. That is exactly the "still observed"
versus "dropped out of the feed" distinction the seam was written to
preserve.

### Aliases: added on measurement, not anticipation

The first run produced exactly one resolver miss — The Odds API's
`Joshua Palmer` against nflverse's `Josh Palmer` (`GSIS:00-0036988`) —
costing 11 otherwise valid quotes. The response was **not** a
`Josh ↔ Joshua` rule: that same game contained a **Josh Allen**, and
generalising from one observation is how a resolver stops being
trustworthy.

Instead, `app/rosterdata/aliases.py` holds one source-controlled entry
keyed `(provider, normalized spelling) → GSIS id`. It resolves to a stable
identity rather than another display name, so it cannot chain; the target
must still be present in the event's own two-team roster pool, so it
cannot pull a player into a game he is not in; and exact matching always
runs first, so it can never override a real roster name. The second run
resolved 16 of 16 with zero unresolved.

### What this does NOT close

**The live quote-freshness tolerance is still owed**, and neither run can
set it:

- Run one offered vendor `last_update` ages. The seam forbids deriving
  eligibility from those — a market untouched for an hour and successfully
  re-fetched a second ago is a *fresh observation of a quiet market*.
- Run two offered observation age, which `live_ingest` forces to exactly
  `0.0` because it takes the snapshot at the quotes' own `as_of_at`. That
  number was measured against itself.

Observation age only becomes meaningful when `taken_at` comes from a real
`CheckpointRun.target_time` with quotes fetched earlier. **That is the one
remaining Week-0 market-data gate**: wire real checkpoint consumption,
measure `checkpoint time − latest eligible as_of_at`, then freeze the
tolerance. It is owed explicitly, not dissolved into "ingestion works".

Also unchanged: Phase 4B historical reconstruction remains out of scope
and needs the paid tier. And Phase 4 still makes only the **market**
component real — recent player stats, injuries, news and team context are
still mocked Phase-2 payload content, so no `EvidenceSnapshot` may be
called "fully real" merely because its `MarketSnapshot` is.

### Test count

**214 passed, 4 skipped.**

---

## Phase 4A.3 readout — quote freshness is now a decision the snapshot records

Phase 4A.2 left one thing explicitly owed: the live quote-freshness
tolerance. 4A.3 does not set it. It builds the mechanism that can, and
makes every decision the mechanism takes visible on the row it took it on.

### The correction that opened the phase

The 4A.2 readout said to measure `checkpoint time − latest eligible
as_of_at`, and elsewhere named that clock `CheckpointRun.target_time`.
That is wrong, and it is the kind of wrong that would have looked fine in
production.

`target_time` is **scheduling intent** — where a capture wanted to land.
`captured_at` is when it actually ran. A capture that fires an hour late
is still a real capture, and the observations it consumed are stale only
relative to when it ran. Measuring against `target_time` would have
reported the scheduler's lateness as market staleness and excluded quotes
that were perfectly fresh at capture.

Two quantities, named apart so they cannot be merged by accident:

| metric | formula |
| --- | --- |
| quote observation age | `MarketSnapshot.taken_at − selected PropQuote.as_of_at` |
| scheduler offset | `CheckpointRun.captured_at − CheckpointRun.target_time` |

`capture_checkpoint` passes one `now` into both `build_snapshot(taken_at=)`
and `run.captured_at`, so the snapshot clock and the capture clock are the
same value by construction, not by convention.

### What the gate does

Freshness is evaluated **per selected sportsbook quote** — the newest
observation from each book as of `taken_at` — on observation age and
nothing else. `provider_market_updated_at` is never consulted.

- A **stale non-canonical book** is excluded from the whole snapshot:
  consensus, `market_median/min/max_line`, and `number_of_books`. Its
  `PropQuote` row is untouched — exclusion is a read-side decision, never
  a retraction of an observation we genuinely made.
- A **stale canonical book** invalidates the baseline and promotes nobody
  (RULES.md §16). The fresh books still supply market context; that is
  diagnostic, not a baseline.
- `canonical_quote_stale` is recorded **separately** from
  `is_valid_canonical_baseline = false`. "The book was quoting, we just
  had nothing recent enough" and "the book was not in the feed at all" are
  different failures and only one is an ingestion problem.

### Four new columns, and why each one exists

```
max_observation_age_seconds   the threshold IN FORCE when this snapshot was built
stale_books_excluded          how many books the gate refused
canonical_quote_stale         whether the canonical book was one of them
selected_quotes  (JSONB)      the exact PropQuote ids consumed and refused
```

`max_observation_age_seconds` is not bookkeeping. Without it, changing a
season's tolerance later makes every historical snapshot's include/exclude
decision unreproducible — you can no longer tell which rule produced it.
Pre-4A.3 rows keep `NULL`, which is the honest value (no gate was in
force) and is **not** the same as `0`.

`selected_quotes` is frozen into the row rather than re-derived. The query
filters `as_of_at <= taken_at`, which *looks* reproducible. It is not: a
Phase 4B backfill can legitimately insert an observation whose `as_of_at`
predates a snapshot already taken, and the same query then returns a
different answer for the same snapshot. There is a test that does exactly
this and asserts the stored record does not move.

**Invariant:** `number_of_books + stale_books_excluded` equals the number
of books that had any quote. A book must never fall out of both counts —
that is how "the feed dropped a book" becomes a snapshot that merely looks
thin.

### There is deliberately no default threshold

`max_observation_age_seconds` is a caller-supplied parameter on
`MarketSnapshotService`, `capture_checkpoint`, and `live_ingest`
(`--max-observation-age-seconds`). `None` disables the gate and is the
default, reproducing Phase-2 behaviour exactly.

It is **not** a `SeasonRules` column yet, and that is the point. The two
real DET @ BUF runs are 15h51m36s apart, which says what a *missed*
refresh looks like and nothing about what a normal one does. A
plausible-looking default picked now would be a guess frozen into the
research record. The mechanism is observable first; the number comes from
real captures.

### Acceptance

Twelve integration tests against real Postgres, **zero Odds API credits**.
They use the two genuine observation timestamps from the live runs
(`04:19:20.012177Z` and `20:10:56.225032Z`) with captures placed at
controlled times against them — a fixture of "now" and "now minus ten
seconds" would pass without ever exercising a realistic staleness spread.

The tests were checked by mutation, not just by passing:

| break the implementation | what went red |
| --- | --- |
| boundary `>` → `>=` | the inclusive-boundary test |
| age read from `provider_market_updated_at` | the vendor-field test |
| promote a fresh book when canonical is stale | both canonical-staleness tests |
| omit the selection record | 11 of 12 |

The migration (`d2b9f45c1a7e`) was applied to a clean database through the
full chain and round-tripped down and back up; the resulting schema shows
no drift against `Base.metadata` and no leftover `server_default`.

### Still owed

The tolerance itself. That needs live captures where quotes were fetched
meaningfully earlier than the checkpoint fired — which the mechanism now
records, and previously could not.

Also open, and deliberately NOT decided here: whether a stale-book
exclusion should be visible in the `EvidenceSnapshot` payload the
competitors see. Today it is not — a model shown `number_of_books: 3`
cannot tell that two books were dropped. That may well be the wrong
answer, but the evidence payload is prompt-versioned and frozen per
season, so changing what competitors are shown is a competition decision,
not an ingestion one. It is recorded on the snapshot either way; only the
hand-off is undecided. (The canonical market already reads as null when
the canonical book is stale, so nothing is silently substituted in the
payload.)

Unchanged from 4A.2: Phase 4B historical reconstruction is out of scope
and needs the paid tier, and Phase 4 still makes only the **market**
component real. Recent player stats, injuries, news and team context are
still mocked Phase-2 payload content.

### Test count

**226 passed, 4 skipped.**

---

## Phase 4A.4 readout — the contract for what happens when the refresh fails

4A.3 built a freshness gate. 4A.4 answers the question the gate was
missing: *fresh relative to what workflow?* The answer reframes the
threshold itself.

### The reframing

A quote-age distribution sampled from arbitrary captures is not evidence
about the tolerance. In the production choreography the refresh
immediately precedes the capture, so a healthy cycle lands observation
ages at ~0 and **every candidate threshold scores identically**. Scenario
A shows exactly that: 900s, 3600s and 7200s are indistinguishable.

The tolerance is an **operational fallback grace period** — when the
refresh that should have preceded this capture failed, how old are we
willing to let the last successful observation be? Only the failure cases
speak to it, which is why the acceptance suite is built around failures
rather than around a measured distribution.

### The choreography

```
refresh market data          network, OUTSIDE any DB transaction
    |
    v
persist immutable PropQuotes, COMMIT
    |
    v
capture checkpoint           DB reads only, at the real captured_at
```

`run_checkpoint_cycle` is the one scheduler-ready entry point — a callable
job, not a scheduler. Three properties it exists to guarantee:

**No provider HTTP inside `capture_checkpoint`.** A network call inside a
capture holds a transaction open across an unbounded wait, and a timeout
aborts a capture that has already written half a slate. The guard walks
the *transitive* import graph from the capture path, because the dangerous
version of this regression is a helper three modules down growing an
adapter import. It's proven non-vacuous by pointing the same walker at
`live_ingest`, which legitimately does reach a provider.

**The capture clock is read after the refresh settles.** Reading it first
reproduces the 4A.2 bug exactly — snapshots that cannot see the quotes the
run just wrote. There's a test asserting the call order, not just the
outcome.

**A failed refresh does not abort the capture.** A missing checkpoint is a
hole in the record; a stale-but-labelled one is information.

### Calibration never captures

`capture_checkpoint` is idempotent once CAPTURED. Capturing the durable
DET @ BUF FINAL to try a candidate threshold would permanently freeze
experimental artifacts onto a real game with no second attempt. So the
preview writes nothing — no `MarketSnapshot`, no `CheckpointRun`, no
`EvidenceSnapshot` — and a structural test enforces it.

It also doesn't re-implement the rule, which was the more interesting
constraint. A preview built on its own copy is evidence about the copy.
The rule is now one pure function (`quote_selection.plan_selection`) over
plain value objects, called by both `build_snapshot` and the preview,
with:

- a test asserting preview and real capture reach identical verdicts on
  identical data;
- an AST guard asserting `quote_selection.py` is the only module in the
  codebase that ordering-compares an observation age to a tolerance;
- `provider_market_updated_at` simply absent from `QuoteObservation`, so
  the rule cannot consult the forbidden field even by accident.

The guard is narrowed to ordering operators — `is None` on a tolerance is
a presence check, which renderers legitimately do. Both guards were
verified by injecting a shadow implementation and watching them fail.

### Scenarios A–G

| | scenario | result |
| --- | --- | --- |
| A | refresh succeeds immediately before capture | ages 0.0; no candidate distinguishable |
| B | refresh fails, observations 600s old | captured, baseline valid, labelled |
| C | refresh fails, canonical 3600s old | baseline invalid, `canonical_quote_stale`, nothing promoted |
| D | one comparison book stale, canonical fresh | 1 excluded, context narrowed |
| E | no fresh comparison books | canonical-only: `books_observed` 4 vs `number_of_books` 1 |
| F | scheduler 3000s late, quotes fresh | large offset, ages 0.0 |
| G | scheduler on time, earlier refresh failed | offset 0.0, ages 5400 |

F and G are the pair that matters: opposite readings from the same two
fields. If anything ever derived one clock from the other, one of them
would break.

### Competitors are told coverage was degraded — not what was rejected

`number_of_books: 3` is ambiguous: three books existed, or five existed
and two were refused. That's degraded coverage versus naturally thin
coverage, and presenting the first as the second presents a feed problem
as a market fact. The shared evidence payload and the request
`MarketContext` now carry `number_of_books`, `books_observed`,
`stale_books_excluded`, `canonical_quote_stale`.

Deliberately absent: the stale quotes' prices, which books they came from,
and the vendor's `last_update`. Competitors learn the evidence was
degraded and by how much; they don't get the rejected prices back through
a side door, and never a field the rule itself is forbidden to use.

`BENCHMARK_PROMPT_VERSION` moves **v2 → v3** with instruction text
explaining the counts — the v1→v2 lesson applied directly, since three
providers all independently misread `confidence` when its meaning lived
only in a schema keyword. `FORECAST_SCHEMA_VERSION` stays at
**forecast-v1**: the response schema is untouched, and bumping it would
falsely invalidate every stored forecast's shape contract. The renderer
refuses a v2 request, so a running season can't silently acquire v3's
fields.

A new `books_observed` column plus two CHECKs make the invariant the
database's problem rather than a test's: `max_observation_age_seconds >= 0`
when non-null, and `books_observed = number_of_books + stale_books_excluded`.
A book must never fall out of both counts.

### Config validation

`None` disables the gate; any threshold must be a non-negative int. A
negative value isn't a strict rule — it marks everything stale, so the run
reports a total market outage that never happened. Rejected at CLI parse,
at service construction (before a run starts, not partway through a
slate), and by the DB CHECK. `bool` is rejected too: `True` is an `int`
and would become a one-second tolerance. `run_checkpoint_cycle` validates
before calling `refresh`, so a typo can't spend a provider call first.

### Recommended threshold — proposed, not frozen

**900 seconds, one value for all three checkpoint types.** Full argument
in `docs/phase4a4-threshold-recommendation.md`.

The honest part of that recommendation: **900s does not keep a checkpoint
alive through a refresh failure, and no value can.** With one refresh per
checkpoint, a failed refresh means the last observation is from the
previous checkpoint — the real Week-3 runs were 15h51m apart. Any
tolerance below the inter-checkpoint gap turns one failed refresh into a
lost checkpoint, and any tolerance above it accepts yesterday's market as
today's.

So 900s isn't chosen to save the checkpoint. It's chosen to make the
failure loud and correctly labelled rather than producing a
confident-looking snapshot built on a stale baseline. If we want
checkpoints to survive refresh failures, the fix is retries and a second
attempt inside the window — a 4A.5 question, not a number.

Nothing in the code carries 900. The gate is opt-in, defaults to `None`,
and `SeasonRules` has no tolerance column; the proposed field shape and
the migration plan are in the doc, gated on acceptance.

### Test count

**276 passed, 4 skipped.** Migrations `d2b9f45c1a7e` and `e5c1a93f2b64`
applied through the full chain on a clean database and round-tripped
twice; no drift against `Base.metadata`.

---

## Phase 4A.5 readout — cost integrity, and retries before an irreversible capture

Two problems with 4A.4, one of them a real bug in shipped code.

### The bug: the cycle paid before it knew whether it could use the data

`run_checkpoint_cycle` called `refresh()` first and only then entered
`capture_checkpoint`, where CAPTURED / too-early / expired were handled.
So a scheduler that re-ticked on an already-CAPTURED checkpoint bought
quotes that could never reach an immutable row — and paid again on every
tick. Same for a poll before the window opens or after it closes.

The preflight now runs first, is read-only, and gates the provider call.
Four dispositions, one of which may spend money:

```
ALREADY_CAPTURED  immutable; new quotes could never reach it   -> 0 calls
TOO_EARLY         will not capture yet                          -> 0 calls
EXPIRED           will not capture at all (MISSED path runs)    -> 0 calls
ELIGIBLE                                                        -> refresh
```

The eligibility rule now lives in one pure module used by *both* the
preflight and `capture_checkpoint`. Two copies would let them disagree,
and the disagreement would be paid for in credits on every tick — there's
an AST guard asserting neither module re-derives the window arithmetic.

The preflight deliberately writes nothing, not even the PENDING row.
Creating state as a side effect of asking a question would mean a
too-early poll silently changed the record it was only meant to read.
`capture_checkpoint` still does that bookkeeping afterwards, unchanged.

### Retries: Model B, because capture is irreversible

`capture_checkpoint` is idempotent once CAPTURED, so "retry by calling the
cycle again later" can't work — the first call has already consumed the
checkpoint. One transient timeout would permanently cost FINAL, the
highest-value checkpoint of the three. So attempts are bounded, explicit,
and happen *inside* the same call, before the capture.

Proposed budget: **3 attempts, backoff 30s then 120s.** FINAL's window is
4 hours wide, so 150s is negligible against it; worst case is 3× the
event-odds call and only on failure. Three attempts can't fix a sustained
outage — and a sustained outage is exactly when we shouldn't be
forecasting on last-known prices.

Two constraints beyond the brief:

**Not every failure is retryable.** `AUTHENTICATION_ERROR`,
`QUOTA_EXHAUSTED` and `QUOTA_RESERVE_EXHAUSTED` can't succeed on a retry.
`MALFORMED_RESPONSE` is the sharpest case: the provider *answered* and we
were **billed**, so the same request returns the same unusable payload and
a retry pays again for it.

**Retries never consume the window they protect.** A guard stops the loop
when the next backoff would leave too little room to capture. Turning a
recoverable failure into a MISSED checkpoint is worse than the failure.

Each attempt is its own provider call with its own `ProviderCall` row and
appears separately in the report. No adapter-level retry — that would make
three billed calls look like one, and "how many times did we ask, and what
did each attempt say" is research provenance.

### Retraction: the 900s rationale was partly wrong

The previous recommendation said 900s "comfortably covers a refresh that
succeeded but ran slowly, plus a retry or two." That is false.

A current quote stamps `as_of_at` at **response receipt**
(`the_odds_api.py:316`). So a slow-but-successful request still yields age
~0 at capture, and so does a retry that eventually succeeds. The tolerance
is not a timeout allowance and not a retry allowance.

It bites only when the newest usable observation pre-dates the current
attempt entirely: a refresh that failed outright, or a book that vanished
from an otherwise successful response. Under one-refresh-per-checkpoint,
that fallback is the *previous checkpoint* — 15h51m in the real Week-3
data.

Following that through further than the correction required: **900s is
currently close to inert.** It discriminates only inside a band today's
workflow never lands in, so its practical effect right now is identical to
a 0-second tolerance — make a refresh failure loud.

Still recommending 900 rather than 0, for a narrower reason than before:
it's the right order of magnitude for the cases that *do* become real once
any pre-capture cadence exists, and it's a value we can grow into without
a rules amendment. The choice simply doesn't require precision yet.

Scenario B (600s fallback after a failure) is relabelled in the docs as a
**policy scenario**, not an observed operational path.

### The policy is frozen, not chosen per run

`SeasonRules` gains `max_observation_age_seconds` and
`refresh_retry_policy` (migration `f3a71d9b28c4`, both nullable, CHECK on
the tolerance). Both change which market state may reach an irreversible
capture, so neither is an operator flag — two checkpoints in one season
built under different rules with nothing in the record saying so is
exactly what frozen rules exist to prevent.

`CapturePolicy.from_season_rules()` is the only official constructor and
carries `rules_version` as provenance. A directly-constructed policy
(calibration, tests) reports `is_official = False`, so "the season's rules
said so" can't be confused with "someone injected a value for this run".

**No value is set on any season.** NULL means no capture policy frozen —
what every pre-4A.5 season genuinely ran under, and not the same as a
zero-second tolerance or zero attempts. Setting them is one provisioning
call on acceptance; a later change is a rules amendment, never an UPDATE.

### Acceptance

24 new tests, zero credits — the refresh is a counter, so a *failing*
refresh is just a callable returning `ok=False`.

Verified by mutation, five ways:

| mutation | what went red |
| --- | --- |
| restore the 4A.4 ordering (refresh regardless of disposition) | all three zero-call tests |
| CAPTURED no longer blocks a refresh | the already-captured test |
| make every category retryable | all four non-retryable cases |
| remove the window guard | the window-guard test |
| let the preflight create the PENDING row | the writes-nothing test |

Plus: eligible calls *do* refresh (a preflight that refused everything
would pass the three zero-call tests and be useless), at-most-once capture
across retries, and the capture clock read after the *final* attempt.

### Test count

**303 passed, 4 skipped.** Migration `f3a71d9b28c4` applied through the
full chain on a clean database and round-tripped twice.

---

## Phase 4A.5 closeout — three things that would have cost money or lied

### A NULL policy was masquerading as an official one

`CapturePolicy.from_season_rules()` loaded a real-provider season whose
`max_observation_age_seconds` and `refresh_retry_policy` were both NULL,
turned that into "no freshness gate, one attempt", and reported
`is_official = True` because `rules_version` was populated.

That made *the absence of a decision* indistinguishable from *a decision
to disable the gate* — and it did so on the run that looked most
authoritative. The durable BotBet 2026 season is in exactly that state
right now, so this was live.

A real-provider season now raises `CapturePolicyNotFrozen` until both
fields are set, and `is_official` requires every capture-policy value to
have come from the frozen row (tracked by a `retry_frozen` flag, so "the
rules say one attempt" and "no retry policy was ever frozen" stop being
the same object). Synthetic seasons still resolve — they fabricate their
own market data and have no provider to overspend against — but report
`is_official = False`.

Frozen JSON is now strictly validated rather than coerced. `int("3")`,
`int(True)` and `int(3.7)` all succeed in Python; unknown keys are
rejected too, since a misspelled field silently takes its default.

### The concurrency hole, and two bugs the race test found in my fix

Two workers could both see ELIGIBLE and both pay. `checkpoint_lease.py`
makes the claim atomic — one `INSERT ... ON CONFLICT DO UPDATE ... WHERE`,
committed before any network work, with no lock held across the provider
call.

Worth recording that my first implementation was wrong twice, and the
test caught both:

**I released the lease after the refresh, not after the capture.** That
leaves a window where a worker has finished paying but has not yet
captured — and a second worker claiming in that window re-verifies
ELIGIBLE and pays again. It is the *capture* that makes a checkpoint stop
being eligible, so the lease has to span it.

**A takeover kept the abandoned row's primary key.** So the worker that
overran its lease still held a matching `lease_id`, and its release
deleted the *new* holder's lease on the way out. `SET id = EXCLUDED.id` is
load-bearing.

Also: the disposition is re-verified under the lease, since the preflight
read is unsynchronized and can be stale by the time the claim succeeds.
And a worker that loses the lease captures nothing either — it has no
fresh data, and capturing would consume the checkpoint out from under the
holder.

The race test runs real threads against real Postgres, because the claim's
atomicity is a property of one SQL statement rather than of application
logic. Disabling the lease fails it deterministically, five runs out of
five.

### The window guard did not cover attempt 1

A cycle starting seconds before `window_end` was ELIGIBLE, could buy a
refresh, and then crossed the boundary — so the capture it paid for landed
as MISSED. One rule (`_has_room`) now covers attempt 1 and every retry;
two separate guards would inevitably disagree.

Sized from the real call sequence rather than assumed: nflverse roster
(60s timeout) + `list_events` (30s) + `fetch_quotes` (30s) = a 180s request
budget, plus a 60s capture reserve — **240s of room needed before any paid
attempt.** A 60s guard would have covered only the odds timeout. There's a
test asserting the default still covers all three timeouts, so a future
change to either adapter fails it.

### Freeze machinery — built, not fired

`amend_capture_policy` clones the active `SeasonRules` row, changes only
the two capture-policy fields plus version/effective_from/reason, and sets
the old row's `superseded_by` — one transaction, append-only. A guard
verifies all 21 methodology fields are identical *after* building the
clone, so a future edit that widened the command fails there rather than
silently rewriting methodology. The CLI is a dry run unless `--apply` is
passed.

`official_capture` resolves game → season → active rules → policy and has
no flags for the tolerance, retry budget, canonical book or provider — a
structural test asserts that.

**Nothing has been applied to production.** The exact command and the
before/after diff are in the session notes awaiting approval.

### Test count

**338 passed, 4 skipped.** Migration `a71e5c3d94f8` (the lease table)
applied through the full chain on a clean database and round-tripped
twice.

---

## Phase 4A.5 final — the official command did not actually refresh

`official_capture.main()` called `run_official_checkpoint` with no refresh.
`run_checkpoint_cycle` treats `refresh is None` as "no refresh supplied"
and proceeds straight to the capture. So the shiny new official command
would have consumed a real, irreversible checkpoint using whatever stale
quotes happened to be sitting in Postgres — the 900s gate would have
invalidated them, and the checkpoint would still be CAPTURED and gone.

That is the whole refresh→capture contract defeated by a default argument.

### What changed

The official runner now resolves the production refresh for the season's
pinned provider alongside the policy. A provider with no refresh wired
raises `NoProductionRefresh` and captures nothing; adding a provider means
adding its refresh, not relaxing the check.

`refresh_game_market_data(game_id, ...)` takes only a game id.
`Game.external_ref` already holds the provider event identity
permanently, so there is no `--event-id` for an operator to get wrong and
no `list_events` call to rediscover something we already know — two
provider calls per logical attempt, not three. (Correctness and failure
surface, **not** cost: `/events` is free on this provider. An earlier
version of this readout claimed the skip saved a credit; it doesn't.)

Which matters for accounting: `provider_calls_spent` sums what each
attempt actually reports rather than counting one per attempt. A retry of
a two-call refresh costs two more calls, and the report says so.

The identity-resolution and quote-persistence loop is now shared with
`live_ingest` rather than copied. A second implementation could drift —
different alias handling, a different quarantine rule — and nothing would
flag it, because both would keep producing plausible rows. `live_ingest`
is a thin acceptance CLI over the shared service, and a test asserts it no
longer names `resolve_and_record` or `resolve_prop_market` itself.

The refresh builds no snapshot, no evidence, and calls no model.

### Amendment hardening

`apply_amendment` re-reads the active rules row **under a lock** and
re-checks its version, so two amendment commands racing cannot both
supersede the same parent and leave two active clones. The CLI's dry run
now prints the exact `--apply --expect-current-version <parent>` line to
re-run with, so the row you reviewed is the row that gets superseded.

### One design note found while testing

`run_official_checkpoint` accepts `refresh` and `sleep_fn` as test seams
but has **no `now_fn`** — an official capture must not be told what time it
is. The retry tests move the kickoff instead of the clock.

### Acceptance

349 passed, 4 skipped. Verified by mutation:

| mutation | what went red |
| --- | --- |
| restore `refresh=None` in the official runner | the fail-closed test |
| allow an unwired provider | the fail-closed test |
| count one ProviderCall per attempt | the multi-call accounting test |
| ignore the expected amendment parent | both amendment-race tests |

---

## Phase 4A.6 — registration, and a claim I got wrong

### The correction

I said `inspect_ingestion` could tell us whether the DET@BUF checkpoints
captured. It couldn't: the file had zero mentions of `CheckpointRun`. It
reported identity rows, quotes, observation times and market movement, and
nothing about checkpoint state. That was a claim about code I wrote and
didn't check.

It now reports, per checkpoint type: row existence, status, window,
target, `captured_at`, scheduler offset, and the `EvidenceSnapshot` /
`MarketSnapshot` counts reachable from that run. Plus any open cycle
leases.

**NONE is reported as NONE.** A checkpoint that was never created and one
explicitly marked MISSED are different facts — the first is historical
absence, the second is a decision the system recorded — and collapsing
them would let a gap in the record look like a logged outcome. There are
tests for both directions.

### Registration: the gap the official runner's strictness created

`official_capture` deliberately won't rediscover an event — it rebuilds
the provider identity from `Game.external_ref`, so a capture can never be
pointed at the wrong game by a typo. Good for capture integrity, but it
means something else has to create the `Game` rows.

`register_week_events` does that and nothing else:

```
MAY       call /events, canonicalize teams, create Game rows,
          verify existing Game rows against their full scope
MUST NOT  fetch props or roster, create Player/GamePlayer,
          PropMarket/PropQuote, MarketSnapshot/EvidenceSnapshot,
          CheckpointRun, or call a model
```

That list is enforced by a structural test and by a test asserting the
tables stay empty. `/events` is free on this provider, so registration is
re-runnable as the schedule changes — but the provider call is still
recorded, because a free call is still a call we made and the audit chain
shouldn't have holes just because a row costs nothing.

The provider comes from the season's active `SeasonRules`, never a flag: a
registration run using a different feed from the one the season is pinned
to would create `Game` rows no capture could ever refresh.

Scope verification is now shared with `live_ingest` rather than copied —
same argument as quote selection and the resolution loop. A test asserts
`live_ingest` no longer contains its own `EVENT_SCOPE_CONFLICT`.

### On testing a race honestly

The threaded concurrency test proves the invariant — never two rows for
one `external_ref`. It did **not** prove the `IntegrityError` recovery
branch: with the GIL, two workers routinely finished sequentially, so a
mutation deleting that handler still passed. Raising it to eight workers
helped but stayed flaky — 1 failure in 3.

So the branch now has its own deterministic test using REPEATABLE READ: a
transaction's snapshot is fixed at its first read, so A reads nothing, B
commits, and A's insert collides for certain rather than by luck. 3/3 pass
clean, 3/3 fail under mutation.

Worth stating because the first version looked like coverage and wasn't.

### Test count

**388 passed, 4 skipped.** No migration.

---

## Phase 4A.6 correction — "week 4" meant "whatever's in the next 8 days"

The registration CLI said `--week-number 4`. The selection rule said
"every event between now and eight days from now". Those are not the same
thing, and `Game.week_number` is part of a permanent identity scope.

### What the command I handed over would actually have done

Run on 2026-09-18 with `--week-number 4 --days-ahead 8`:

| | |
| --- | --- |
| discovery window | 2026-09-18 .. 2026-09-26 |
| real Week 4 | 2026-10-01 .. 2026-10-05 |
| games it would have swept | 15 × Week 2, 1 × Week 3 |
| real Week 4 games captured | **0** |

Sixteen events, all permanently stamped Week 4, none of them Week 4.

### The same mistake had already been made by hand

Checking the schedule turned up something else: our own README records
DET @ BUF as **week 3**. nflverse says that fixture (2026-09-17) is
**week 2** — week 3 runs 2026-09-24..09-28. The Phase 4A.2 acceptance run
passed `--week-number 3` and nothing verified it.

So the durable research record already contains a mis-scoped game. I have
**not** touched it: `Game.external_ref` is permanent and silently
rewriting `week_number` is exactly the repair this design forbids. It
needs a decision, not a patch.

### The fix

`app/scheduledata/` defines a provider-neutral schedule seam;
`NflverseScheduleProvider` implements it from the same free release base
the rosters already use (`schedules/games.csv`), so no new vendor and no
cost.

`resolve_event_week` is pure and matches on the **ordered** (away, home)
pair within a season. Division rivals meet twice but once at each venue,
so the ordered pair identifies a fixture while the unordered pair would
collapse both meetings. Five outcomes, one of which may persist:

```
MATCHED               the schedule says this IS the requested week
OTHER_WEEK            refused; the real week is reported
UNKNOWN_FIXTURE       refused
AMBIGUOUS_FIXTURE     refused
KICKOFF_DISAGREEMENT  refused beyond a 3h tolerance
```

The tolerance is wide enough for a rounded or provisional broadcast time
and narrow enough that a flex move to another slot or day is refused —
which is right, because a disagreement that large means either a stale
schedule or a mismatched fixture, and both deserve a human.

An unusable schedule is fatal: without an authoritative week there is
nothing to verify against, so nothing is written. The schedule is fetched
**before** the events call, and a test asserts the events call never ran.

**Preview is now the default.** `--apply` is required to persist. A
discovery mistake must never be able to create an immutable row.

The discovery window default moved 8 → 21 days, but that is not the fix
and must not be read as one: the window only decides which events we ask
about. Anything outside the requested week is refused, not relabelled.

### Also fixed

The inspector called every `CheckpointCycleLease` row an "open cycle
lease" without checking expiry. A crashed worker leaves one behind until
the next claim reclaims it, and describing that as blocking would send
someone hunting a worker that isn't running. Now `ACTIVE` vs `EXPIRED`,
with the expired ones marked reclaimable.

### Acceptance

405 passed, 4 skipped. Mutation-tested four ways — deleting the week
guard, ignoring the resolution, making preview write, and reporting
expired leases as active — each turned the intended tests red.

The headline test reconstructs the original poisoning scenario directly:
a window full of other weeks' games with week 4 requested, and asserts
zero rows written.

---

## Phase 4A.6 closeout — provenance for the field that decides identity

Four gaps, all found in review of the week-verification fix.

### The schedule decision had no durable provenance

`/events` was persisted. The nflverse schedule fetch that actually decides
`Game.week_number` was consumed and thrown away. So we could prove which
event we saw and not which snapshot classified it — on the one field being
treated as permanent identity scope.

`game_scope_observations` now records, per successful registration: both
provider call ids, the resolved week, the canonical teams, both kickoffs,
the drift between them, and the resolver version. Append-only — a later
pass adds another row rather than rewriting the first, so "what did the
schedule say each time we looked" stays readable.

Recorded for schedule *failures* too: "the schedule was unreachable at
14:03" is itself the answer to why a pass wrote nothing.

A foreign key caught a real bug while wiring this: I linked the
observation to the `IngestionRun` id rather than the `ProviderCall` id.

### The schedule source ignored the frozen pin

Registration built `NflverseScheduleProvider` unconditionally and checked
only `market_data_provider`. Correct for this season by luck; silently
wrong for any other.

V1 rule, explicit: **schedule identity follows the frozen
`roster_data_provider`.** Both answer "who is this and where does it sit
in the season", so splitting them across vendors would let a game be
classified by one source and its players resolved by another. Fails closed
if the pin has no schedule implementation.

### 3 hours was the wrong tolerance, for a reason I'd missed

I justified 3h against *week* identity: any drift under a day still lands
in the same week, so the label stays right. That reasoning was incomplete.

`Game.kickoff_at` drives the OPENING/MID/FINAL windows. Accepting a 2h45m
disagreement produces a correctly-labelled week whose entire research
clock is 2h45m wrong — a FINAL window targeted at kickoff minus three
hours would fire at a time that means nothing. The tolerance has to match
the timing precision the checkpoint system claims, not week arithmetic.

**Now 15 minutes.** The preview prints every fixture's market-vs-schedule
drift, including the ones it refuses — a preview that hid the outliers
would hide exactly the cases the tolerance exists to exclude. The real
Week-4 preview can confirm or revise the number before anything is
applied.

### "PREVIEW — nothing written" was not true

Preview persists provider-call audit telemetry, deliberately. The header
now says: *no Game rows written; provider audit telemetry recorded.*

### DET @ BUF: repair, not preservation

I said we shouldn't touch it because `external_ref` is permanent. That
conflated two things. Silent repair is forbidden; **explicit, audited
correction is not**, and leaving a known-false week in the durable record
forever is not better integrity — it's a different way to be wrong, with
no paper trail either way.

`repair_game_week` corrects `week_number` only, in one transaction, after
appending a `game_scope_corrections` audit row. `Game.id`, `external_ref`,
teams and `kickoff_at` are never assigned, asserted structurally. Dry run
by default.

### The repair may not take the week from the operator either

The first version accepted `--authoritative-week 2` and trusted it. That
reproduced, inside the repair tool, the exact defect the repair exists to
undo: Phase 4A.2 wrote week 3 because a human typed 3. A correction
sourced from a second human guess is not a verification — it's the same
mistake with a nicer audit row.

The flag is gone. The repair resolves the week itself, through the
season's own frozen pins and the **same matcher registration uses**:

    Game -> Season -> active SeasonRules -> frozen roster_data_provider
         -> schedule implementation -> fetch schedule (call PERSISTED)
         -> resolve_fixture(away, home, kickoff_at)

`resolve_event_week` was split so `resolve_fixture` answers the open
question — *what week IS this fixture* — with no request to compare
against. Registration is now a thin wrapper that adds the `OTHER_WEEK`
check. A repair that derived the week by a second route could disagree
with the rule that classified every other game in the season.

`--expect-authoritative-week` exists but is a **guard, not an input**: it
asserts what you expect the schedule to say and refuses on disagreement.
It can only ever make the repair do less. An `UNKNOWN_FIXTURE`,
`AMBIGUOUS_FIXTURE` or `KICKOFF_DISAGREEMENT` verdict is never corrected —
`kickoff_at` is not repaired here and it drives the checkpoint windows, so
relabelling the week while leaving a disputed clock is half a repair.

`game_scope_corrections` now carries a CHECK constraint: a `week_number`
correction with a NULL `schedule_provider_call_id` is rejected by the
**database**, not only by the tool. The tool isn't the only thing that can
reach that table, and a correction that can't name its schedule snapshot
is indistinguishable from the hand-typed week it replaces.

No post-correction `GameScopeObservation` is appended, deliberately: that
table requires a market provider call too, because an observation is a
statement about an event we fetched. A repair fetches no event.

### Apply locks the row and re-verifies everything

The dry run and the apply are separate processes minutes apart, and the
network call happens before either. So the apply takes the `Game`
`FOR UPDATE`, then re-runs the expected-week check and the **whole
dependency census inside the lock**. No network happens there — the
schedule was already fetched and its call already persisted — so the lock
is bounded by local queries. Two concurrent repairs produce exactly one
correction: the loser sees the corrected row and refuses.

### MarketSnapshots are classified, not blanket-blocked

Blocking on any `MarketSnapshot` at all was too blunt. A snapshot is a
derived read of quotes that hang off `game_id`, and `game_id` does not
change. What makes an artifact unsafe to relabel is something having
*committed* to it — so the census classifies rather than counts, and a
standalone snapshot and a PENDING checkpoint do not block.

### The census called itself complete and covered five models

It scanned checkpoints, snapshots, evidence, forecasts and `AgentSession`'s
two singular FK columns. That is not the set of things that can commit
this game to a week. `BenchmarkSlot` carries a `game_id` and a plan tied
to a `week_id`; `Ticket`, `Wager` and `PassDecision` carry both a
`market_id` and a `week_id`; `StakeRecommendation` and
`ResearchSettlement` carry the market; `Settlement` and
`BankrollTransaction` sit one hop past a wager and represent real money.
Correcting `Game.week_number` under any of them produces exactly the
contradiction the repair exists to eliminate:

    Game.week_number = 2   while   Ticket.week_id -> Week 3

All twelve are traversed now, and the report prints the week numbers the
committed artifacts actually name, so a contradiction is visible rather
than inferable.

**Batched model calls were invisible.** `agent_session_evidence_snapshots`
is the source of truth for multi-market inputs — `create_pending` says so
in as many words, and it never populates either singular column. Counting
only `evidence_snapshot_id` / `market_snapshot_id` therefore reported
`agent_sessions = 0` for *every* batched call that consumed this game.
The census now takes a DISTINCT union across all three routes.

**`max(a, b)` was a lower bound, not an upper one.** Two disjoint
reference sets of 2 and 3 reference five snapshots, not three — so
`referenced_snapshots` was understated and `standalone_snapshots`, the
number the safe/unsafe call reads, was *overstated*. Replaced with a real
`DISTINCT` union over evidence, agent sessions and tickets.

### The census now audits itself

`tables_reaching_a_game()` computes the transitive FK closure from live
metadata, and a test asserts every table in it appears in
`CENSUS_CLASSIFICATION` as BLOCKING, OBSERVATIONAL or AUDIT. A model added
later that can reach a game turns that test red until someone classifies
it deliberately. It immediately caught two tables the hand-written list
missed — `ingestion_runs` reaches games through
`IngestionRun.checkpoint_run_id`, and `provider_calls` through it. A second
test asserts every BLOCKING table is actually queried by `take_census`, so
a classification cannot decay into a comment.

Every counted artifact is printed whatever its count: *we looked and found
none* and *we never looked* must not render identically.

### A correction to the apply ordering comment

The audit row is inserted before the `Game` update, and the comment
claimed that ordering meant the audit row would survive a failed update.
It would not — both statements are in one transaction, so the INSERT rolls
back with the UPDATE. The order is kept for a smaller, real reason: the FK
and CHECK on `game_scope_corrections` are evaluated at that flush, so a
correction that cannot name its schedule call fails *before* `Game` is
touched.

### A refusal is an outcome, not a crash

The first live refusal — a second dry run after the DET correction had
already landed, so the row said 2 when the guard expected 3 — printed a
Python traceback. The guard worked exactly as designed and the output read
like the tool broke.

Refusal is this tool's normal, designed outcome: a stale expected week, an
unresolvable fixture, a disputed kickoff, an unreachable schedule, a
no-op. `main` now catches `RepairRefused` and `ScheduleSourceUnavailable`
at both the plan and the write step, prints `REFUSED — nothing written`
with the wrapped reason, and returns exit code 1. The apply step gets its
own handler because it re-checks under the row lock and can still refuse
after a clean plan.

`main` also gained the `schedule_provider` test seam that
`register_week_events` already had. Without it, a test of this entry point
fetched the live nflverse release — slow, network-dependent, and quietly
coupled to the real 2026 schedule agreeing with its fixtures. No CLI flag
reaches the seam; the season's frozen roster pin still chooses the
implementation in production.

### "If the windows are 144/96, then roughly..." is a guess

That sentence kept appearing whenever the question was *when does the next
capture window open*. It is a guess about production dressed as
arithmetic. The frozen `checkpoint_windows` live in the season's active
`SeasonRules` and are the only thing that decides when a capture may run,
so `app.marketdata.checkpoint_schedule` reads them and projects the real
timestamps. Read-only, zero credits: it may read SeasonRules, Game and
CheckpointRun rows and fetch the free schedule, and it may not touch the
odds provider or write anything.

The window arithmetic is imported from `checkpoint_window`, never
reimplemented — a second copy could disagree with the cycle it is supposed
to be predicting, and a projection you cannot trust is worse than none.

It exists for a specific time-boxed decision. `BenchmarkSlatePlan.week_id`
is UNIQUE and the plan is committed once per week, *before any game's
OPENING window opens*. That makes the earliest OPENING start a deadline
you cannot renegotiate, and it should not be a mental calculation done
under time pressure.

**Fixtures come from two places on purpose.** Registered `Game` rows are
authoritative. The schedule fills in fixtures that exist in the real world
but have no `Game` row yet — because the deadline is computed over *every*
fixture, registered or not. A deadline computed only from registered games
moves later each time the provider is slow to list one, which is exactly
backwards: the unlisted game is the reason to hurry. The week-3 preview
found this live — The Odds API listed 14 of nflverse's 16 fixtures, with
LAC @ BUF and NYJ @ DET missing.

## Phase 4A.7 — the benchmark pool was `list[Game]`

`commit_benchmark_slate_plan` said it committed *using only the schedule*
and then required `games: list[Game]`. A `Game` exists only once THE ODDS
API has posted the event, so the market provider's posting horizon
silently decided which fixtures a precommitted research sample could draw
from. That is how two unlisted Week-3 events could change a slate nflverse
already knew all sixteen fixtures for.

The allocator also ordered by `(kickoff_at, str(Game.id))`, and `Game.id`
is `uuid.uuid4()`. Ten of the sixteen 2026 Week-3 fixtures kick at exactly
the same instant, so four of five slots were decided by UUID ordering —
deterministic inside one database, different after a rebuild.

### One canonical fixture identity

`fixture_identity` defines the key — `season / game_type / week / canonical
away / canonical home` — and everything that needs to say *which fixture
is this* imports it. **Kickoff is deliberately excluded.** Broadcast times
move, fixtures do not, and an identity that changed with the clock would
silently un-bind a committed plan. The week is zero-padded (`W03`) so key
order is numeric order past week 9. `pool_fingerprint` hashes the
canonical ordered keys, version-prefixed, so it survives a clean rebuild.

### The plan owns its fixture pool

`benchmark_slate_fixtures` holds the **complete** pool the allocator saw,
owned by its plan rather than kept in a global table — a frozen historical
artifact, not a second source of truth a later schedule release can
rewrite. `game_id` is NULL until registration binds it, and **binding
moves that one column**. A kickoff inside tolerance binds without writing
itself back; a kickoff outside it is reported as drift and refused. A
fixture the provider never lists is a coverage failure, never a
reallocation.

### One slate writer

The Phase-2B `games=[...]` entry point is now a synthetic adapter that
converts to planned fixtures and delegates to the same core. Asserted
structurally: it may not call `allocate` or construct a `BenchmarkSlot`.
Two independently-correct writers would pass their own tests for months
and disagree in the week it counted.

### The deadline is enforced, and checked on the right clock

`official_slate` takes a season and a week. Slate size, prop types,
allocation method and checkpoint windows all come from frozen
`SeasonRules`. The deadline is the earliest OPENING start across the
**whole** week — a Thursday game nobody picked still ends the precommit
period — and it is checked on the clock read *after* the schedule fetch. A
command that read the clock at startup could begin thirty seconds before
the deadline, spend forty seconds fetching, and commit late while
truthfully reporting it started in time. There is no override flag. A
fixture with no kickoff fails the whole commit: the deadline is computed
from kickoff, so one unknown makes it uncomputable.

### The allocator is not frozen

`SeasonRules.benchmark_allocation_method` is NULL until an amendment
freezes one, and official commitment refuses on NULL *before* spending a
fetch. `ROUND_ROBIN_BY_KICKOFF_V0` is kept only for comparison: with five
slots and sixteen fixtures `games[i % len(games)]` is every index
distinct, so it takes the first five by kickoff — a permanent bias toward
Thursday night and the early Sunday block. Against the real 2026 Week-3
schedule it draws all five from 2 of 6 kickoff blocks.

| Method | Blocks covered (W3) | Survives a 2-fixture pool change |
| --- | --- | --- |
| `ROUND_ROBIN_BY_KICKOFF_V0` | 2/6 | 5/5 |
| `STRATIFIED_BY_KICKOFF_V1` | 3/6 | 2/5 |
| `STABLE_HASH_V1` | 3/6 | 5/5 |
| `KICKOFF_BLOCK_STRATIFIED_V1` | 5/6 | 5/5 |

All four are reproducible under 1,000 shuffled input orderings. **None is
frozen** — that is a methodology decision, not an implementation one.

### Acceptance

538 passed, 4 skipped. Migration `d16b83f9a4c2`. Twenty-five mutations
across the phase; the four that survived the first pass each got a test
and now fail — including binding writing a kickoff back, and an
unversioned hash ranking.

### Earlier acceptance

469 passed, 4 skipped. Mutation-tested twenty-two ways across both passes.
The census half: deleting each of the benchmark-slot, ticket, wager,
bankroll, pass-decision, research-settlement and stake-recommendation
traversals; ignoring `BenchmarkSlot.resolved_market_id`; deleting the
batch join-table route; reverting the union to `max()`; decoupling
`blockers_for` from the census rows; and dropping a table from the
registry — each turned the intended tests red.
