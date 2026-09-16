# Backend — Phase 1 domain, Phase 2 persistence & Forecast Lab core, Phase 3 AI adapter layer

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

**Test count**: 94 passed, 4 skipped (the 4 `live_provider` tests, no
credentials configured in this environment) — up from 80 at the end of
Phase 2. New: 13 pure tests (`test_ai_validation.py`), 1 mock-path
integration test (`test_ai_orchestrator.py`, Postgres), 4 live-provider
integration tests (`test_live_providers.py`, Postgres + real API keys,
currently skipped). Zero regressions in any Phase 1/2 test.

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

**Real provider path status**: implemented and unit-import-clean
(`OpenAIAdapter`/`AnthropicAdapter`/`GeminiAdapter` all construct and
route through the same `AIOrchestrator`/`validation.py`/`session_service.py`
as MockAdapter), but **not live-verified** — this environment has no
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`GOOGLE_API_KEY`
configured, so the 4 `live_provider` tests in `test_live_providers.py`
skip rather than run. Whoever has credentials should run
`pytest -q -m live_provider` (with `DATABASE_URL` set) to complete steps
15–24 of the acceptance test for real; the code path exercising every
one of those steps already exists and passes its mock-equivalent.

**Do not read anything into forecast differences yet** — no live call has
been made in this environment, and even once one is, three models
forecasting one test market is a connectivity check, not a research
result. Model comparison is explicitly out of scope for Phase 3.

**Known Week 0 decisions still intentionally unresolved** (unchanged from
Phase 2, plus the two Phase 3 added): `BATCH_10`/`BATCH_SMALL`/`ISOLATED`
benchmark batching (the schema already supports 1..N markets per call —
`AIOrchestrator._build_market_batch` takes an arbitrary list of
`evidence_snapshot_ids` — but the production choice isn't frozen), and the
exact production model roster/version per provider (the `live_provider`
smoke tests use small/cheap models — `gpt-4o-mini`,
`claude-3-5-haiku-20241022`, `gemini-2.0-flash` — deliberately not
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
