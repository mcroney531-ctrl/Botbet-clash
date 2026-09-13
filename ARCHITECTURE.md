# ARCHITECTURE.md — AI Prop Betting League

Companion to `RULES.md` (rules) and `DATABASE.md` (schema). Defines module
boundaries, service responsibilities, background jobs, and failure
handling for the backend described in the project constitution.

## 1. Style

- **Modular monolith.** One Python/FastAPI codebase, internally divided
  into modules with enforced one-way dependencies. No microservices until
  there is a proven operational reason.
- Modules talk to each other through Python-level service interfaces
  within the process, and through `CompetitionEventBus` for anything the
  Show layer or notifications need to react to asynchronously. Modules
  never share ORM sessions across a service boundary; each service call
  takes/returns domain objects or DTOs, not raw ORM rows, across module
  lines.

## 2. Module map

```
app/
  core/            settings, clock, money (integer-cents helpers), ids
  domain/          pure domain logic: Season, Week, Competitor, Bankroll,
                   Ticket, Wager, Settlement, Kelly/stake math, de-vig math
  data_providers/  OddsProvider, StatsProvider, NewsProvider,
                   InjuryProvider, WeatherProvider, ResearchSettlementProvider
                   (interfaces + normalization; provider-specific adapters
                   live under data_providers/<vendor>/)
  markets/         PropMarket / PropQuote / MarketSnapshot management,
                   consensus + canonical baseline calculation, de-vig
  evidence/        EvidenceSnapshot assembly (freezes market + context state)
  forecast_lab/    ForecastObservation, benchmark slate construction,
                   checkpoint scheduling, Brier/log-loss scoring,
                   common-cohort computation, calibration reports
  ai_orchestrator/ CompetitorAgent interface + OpenAIAgent/ClaudeAgent/
                   GeminiAgent adapters, prompt families, structured-output
                   validation/repair-reject flow
  risk_engine/     Kelly reference, fractional Kelly, stake caps,
                   requested-vs-final-allowed resolution
  competition/     watchlists, Pounce tickets, BET/PASS decisions,
                   weekly orchestration state machine
  execution/       ticket lifecycle, human execution recording
  settlement/      sportsbook settlement, research settlement (separate
                   pipelines), late-stat corrections
  attribution/     MarketEventDetector, AttributionJudge, review queue
  ledger/          BankrollTransaction, bankruptcy detection
  events/          CompetitionEvent model + CompetitionEventBus
  show/            weekly receipts, Call Your Shot, Unanimous Pick, awards,
                   emergent reputations, trash talk (reads events + public
                   state only; never calls into ai_orchestrator directly
                   for handicapping)
  notifications/   NotificationService + channel adapters
  commissioner/    CommissionerService: rules enforcement, deadlines,
                   eligibility, ticket validation, settlement authority,
                   overrides, season config, audit
  api/             FastAPI routers: control-panel API, arena-facing
                   presentation API, SSE event stream
  db/              SQLAlchemy models, session management, Alembic env
```

Dependency direction (left depends on right, never reversed):

```
api → commissioner, show, competition, forecast_lab, execution → 
  risk_engine, markets, evidence, ai_orchestrator, ledger, settlement →
  domain, data_providers, db, events
```

`show/` and `notifications/` may only read from `events/` and read-only
query models exposed by other modules — they must never import
`ai_orchestrator` or call it directly. This is the enforcement point for
constitution §96 ("trash talk ... must never affect later handicapping
prompts") and §4 (competitor independence).

## 3. CommissionerService

The single neutral authority (constitution §9). Owns:

- `SeasonRules` (versioned, immutable once frozen; amendments create a new
  version row, never an in-place update)
- weekly state transitions (`open_week`, `capture_checkpoint`,
  `lock_research_result`, `close_week`)
- deadline enforcement (checkpoint windows, weekly decision deadline)
- ticket validation (stake caps, Pounce limits, schema validation)
- settlement authority (sportsbook + research, kept separate)
- overrides/exceptions (all overrides are themselves `BankrollTransaction`
  / `CompetitionEvent` rows with a reason, never silent mutations)
- awards and historical record computation

No competitor agent grades its own performance; all scoring
(`forecast_lab`, `attribution`) is computed by the Commissioner-owned
pipeline against data the agent cannot write to after the fact.

## 4. Weekly orchestration (state machine)

```
WEEK_OPENED
  → BENCHMARK_SLATE_BUILT
  → OPENING_SNAPSHOT_CAPTURED
  → OPENING_FORECASTS_COLLECTED (all 3 competitors)
  → [ongoing] WATCHLISTS_ACTIVE, OPEN_MARKET_DISCOVERY,
              EVENT_TRIGGERED_REVIEWS, POUNCE_WINDOW
  → MID_SNAPSHOT_CAPTURED → MID_FORECASTS_COLLECTED
  → FINAL_SNAPSHOT_CAPTURED → FINAL_FORECASTS_COLLECTED
  → WEEKLY_DECISIONS_LOCKED (BET or PASS per competitor)
  → TICKETS_ISSUED → HUMAN_EXECUTION_RECORDED
  → GAMES_COMPLETE
  → SPORTSBOOK_SETTLED
  → RESEARCH_SETTLEMENT_LOCKED (T+72h)
  → ATTRIBUTION_CLASSIFIED
  → WEEKLY_RECEIPT_GENERATED
  → WEEK_CLOSED
```

Each transition is driven by the Commissioner control panel (manual
trigger in Week 0 / early season) or a scheduled job (§6) later. Every
transition emits a `CompetitionEvent`.

Checkpoint capture (`OPENING`/`MID`/`FINAL`) is **atomic per constitution
§27**: freeze market + evidence state first, then fan out the identical
snapshot to all three `CompetitorAgent`s, then persist all three
responses against that one `evidence_snapshot_id`. The market must not be
allowed to move underneath the three calls — the orchestrator takes the
snapshot once, before any agent call, and every agent reads from the
persisted snapshot object, not from a live provider call.

## 5. AI orchestration

`CompetitorAgent` (constitution §53) is the only interface `competition/`
and `forecast_lab/` use to talk to a model provider:

```python
class CompetitorAgent(Protocol):
    async def forecast_benchmark(self, slate: BenchmarkSlate, snapshot_ids: list[SnapshotId]) -> list[ForecastObservationDraft]: ...
    async def create_watchlist(self, context: WatchlistContext) -> WatchlistDraft: ...
    async def evaluate_open_market(self, market: PropMarket, snapshot: EvidenceSnapshot) -> ForecastObservationDraft: ...
    async def review_event(self, event: MarketEvent, prior: ForecastObservation) -> ForecastObservationDraft | None: ...
    async def make_final_decision(self, context: DecisionContext) -> FinalDecisionDraft: ...
    async def propose_stake(self, context: StakeContext) -> StakeProposal: ...
    async def generate_public_comment(self, context: PublicContext) -> str: ...
    async def postmortem(self, context: PostmortemContext) -> PostmortemDraft: ...
```

Provider adapters (`OpenAIAgent`, `ClaudeAgent`, `GeminiAgent`) implement
this and own all provider-specific prompt formatting and response
parsing. Every call records: competitor, provider, exact model id,
timestamp, prompt_version, schema_version, evidence_snapshot_id,
market_snapshot_id, raw structured response, validated response, and
bankroll_at_decision (constitution §106) — this is written by the
orchestrator wrapper around every adapter call, not by the adapters
themselves, so it can never be skipped.

**Structured output failure handling** (constitution §54): schema
validation failure → request correction → retry once → reject and log
`AgentCallFailure` if still invalid. Never silently coerce/repair a
consequential field (probability, stake, side).

**Prompt families** (constitution §107) are separate template sets per
call type (`BENCHMARK_FORECASTING`, `OPEN_MARKET_RESEARCH`,
`MARKET_EVENT_REVIEW`, `STAKE_SIZING`, `FINAL_DECISION`, `POSTMORTEM`,
`PUBLIC_COMMENTARY`, `TRASH_TALK`) — kept as separate versioned files so a
change to one never silently touches another.

## 6. Background jobs

Even before a real scheduler exists, these are modeled as discrete,
idempotent job functions so they can be invoked manually (Week 0 control
panel button) or later put behind a scheduler (APScheduler/cron):

| Job | Trigger | Idempotency key |
|---|---|---|
| `capture_checkpoint(week, checkpoint_type)` | window open, manual or scheduled | `(week_id, checkpoint_type)` — no-ops if already captured |
| `poll_market_events(week)` | interval | dedupes via `MarketEvent` fingerprint |
| `run_benchmark_forecast(week, checkpoint)` | after checkpoint capture | `(week_id, checkpoint, competitor_id)` |
| `lock_research_settlement(week)` | T+72h after last game | `week_id` — refuses to re-lock once `research_locked_at` set |
| `settle_sportsbook_wager(wager_id)` | manual (human reports result) | `wager_id` |
| `close_week(week)` | all settlements done | `week_id` |
| `generate_weekly_receipt(week)` | after `close_week` | `week_id` |

All jobs are pure functions over the DB: given the same inputs and current
DB state, re-running is safe (either no-ops or produces the same result).
This matters because Week 0 will run these manually and out of order while
testing.

## 7. Event system

`CompetitionEvent` (constitution §99) is the single append-only stream
every module writes to for anything the Show layer, notifications, or
audit reconstruction need. Modules never poll each other's tables for
"did X happen" — they subscribe to `CompetitionEventBus`.

- In-process: a simple pub/sub dispatcher (sync call-outs within the
  request/job, each wrapped so a subscriber exception never rolls back the
  event write itself).
- Persisted first, dispatched second: the event row commits in the same
  transaction as the domain change it describes; dispatch to in-memory
  subscribers happens after commit. This guarantees the audit log is never
  missing an event even if a downstream subscriber (e.g. Discord
  notification) fails.
- `GET /events/stream` (SSE) is a subscriber that replays new
  `CompetitionEvent` rows to connected arena/control-panel clients.

## 8. Notification flow

`NotificationService` consumes `CompetitionEvent`s (POUNCE_ISSUED,
TICKET_LOCKED, BET_EXECUTED, WEEK_SETTLED, etc.) and fans out to
channel adapters (browser push, mobile push, Discord, email, SMS). The
betting engine never imports a channel adapter directly — it only ever
emits events.

## 9. Settlement flow

Two independent pipelines, never merged (constitution §81–84):

- **Sportsbook settlement**: human reports `sportsbook_result` /
  `sportsbook_payout` / `sportsbook_settled_at` for the actually-placed
  wager. This is what moves real `BankrollTransaction`s.
- **Research settlement**: `ResearchSettlementProvider` supplies the
  official stat at `T+72h`; locked once into
  `research_stat_value_at_lock` / `research_outcome_at_lock` /
  `research_locked_at`. Later corrections are appended as
  `later_corrected_stat` / `later_corrected_outcome` /
  `correction_timestamp`, never overwriting the original lock.

Divergence between the two (constitution §84) is expected and stored, not
reconciled away.

## 10. Failure handling

- **Data provider failure** (odds/stats/news/weather unavailable at a
  checkpoint): the checkpoint is captured with whatever is available;
  markets missing a valid canonical quote are excluded
  (`CANONICAL_MARKET_UNAVAILABLE`) rather than blocking the whole
  checkpoint.
- **Agent call failure**: retry-once-then-reject per §5. A rejected
  benchmark forecast produces `GPT_FORECAST_MISSING`/`_INVALID` for that
  market/checkpoint and the week proceeds without it — one competitor's
  outage never blocks the others.
- **Partial checkpoint**: it is valid for a market to have `OPENING` but
  miss `MID` (line withdrawn, book stopped offering it). Downstream
  scoring treats missing checkpoints as absent, never imputed.
- **Commissioner overrides**: any manual override (re-settle, force-close
  a week, override a stuck ticket) is itself a recorded, reasoned
  `CompetitionEvent` + ledger entry — never a raw UPDATE with no trail.

## 11. Audit strategy

Constitution §106/§116/§120 all converge on one requirement: full
reconstruction of "what every competitor knew, believed, changed,
requested, risked, executed and achieved at every meaningful point."
Concretely:

- Every mutable-seeming concept is actually append-only:
  `ForecastObservation` revisions, `BankrollTransaction`s,
  `CompetitionEvent`s, market snapshots.
- Every AI decision row carries its `evidence_snapshot_id` and
  `market_snapshot_id` foreign keys — never a denormalized copy that could
  drift from the snapshot.
- `AuditLog` is a thin read-side: given a `week_id` (or `competitor_id`,
  or `market_id`), it walks `CompetitionEvent` + the append-only tables in
  timestamp order and renders a full timeline. It contains no business
  logic of its own — if it did, it could disagree with the system it's
  auditing.

## 12. API surface (initial)

Control-panel (constitution §104), internal/authenticated:

```
GET  /commissioner/state
POST /commissioner/weeks/{week_id}/open
POST /commissioner/weeks/{week_id}/checkpoints/{type}/capture
POST /commissioner/weeks/{week_id}/forecasts/run
POST /commissioner/tickets/{ticket_id}/execution
POST /commissioner/wagers/{wager_id}/settle
POST /commissioner/weeks/{week_id}/research-lock
POST /commissioner/weeks/{week_id}/close
```

Arena-facing presentation (constitution §100), public read models:

```
GET /arena/state
GET /arena/events         (SSE)
GET /leaderboard
GET /competitors/{id}
GET /weeks/{id}
```

The arena/presentation layer never reconstructs competition logic itself —
it only renders read models the `show/` module computes from events +
settled state.

## 13. Build sequence

Follows constitution §117 phases 0–13 in order; this repository currently
implements **Phase 1 (domain skeleton, fake markets)**. See `DATABASE.md`
for the schema those Phase 1 models are drawn from.
