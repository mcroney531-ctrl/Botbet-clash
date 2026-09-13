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

## 4. Weekly orchestration (two independent state machines)

**v2 correction:** v1 modeled this as one linear per-week state machine
(`OPENING_SNAPSHOT_CAPTURED → ... → MID_SNAPSHOT_CAPTURED → ... →
WEEKLY_DECISIONS_LOCKED → TICKETS_ISSUED → ...`). That's wrong on two
independent axes and was corrected as follows.

**Axis 1 — Forecast Lab checkpoint progression is per-game, not
per-week.** A week's games kick off at different times (Thursday, Sunday
early/late, Sunday night, Monday) — RULES.md §9 already says checkpoints
are kickoff-relative, so there is no single "the week hit MID" moment.
Each game independently drives its own `checkpoint_runs` rows
(DATABASE.md §4):

```
per game:
  CHECKPOINT_PENDING(OPENING) → CHECKPOINT_CAPTURED(OPENING) → FORECASTS_COLLECTED(OPENING)
  CHECKPOINT_PENDING(MID)     → CHECKPOINT_CAPTURED(MID)     → FORECASTS_COLLECTED(MID)
  CHECKPOINT_PENDING(FINAL)   → CHECKPOINT_CAPTURED(FINAL)   → FORECASTS_COLLECTED(FINAL)
  → GAME_COMPLETE → RESEARCH_SETTLEMENT_LOCKED (T+72h after this game)
```

A `Week` (DATABASE.md §1) tracks only aggregate bookkeeping —
`OPENED → ... → GAMES_COMPLETE → SETTLED → CLOSED` — derived from whether
all its games have reached the corresponding per-game state, not a
checkpoint stage in its own right. `close_week` requires every game in
the week to be `RESEARCH_SETTLEMENT_LOCKED` (or otherwise finalized),
not "the week reached FINAL."

Checkpoint capture is still **atomic per constitution §27** within a
single `(game_id, checkpoint_type)` run: freeze market + evidence state
for every market in that game first, then fan out the identical snapshots
to all three `CompetitorAgent`s, then persist all three responses against
those `evidence_snapshot_id`s. The market must not be allowed to move
underneath the three calls — the orchestrator takes the snapshot once,
before any agent call, and every agent reads from the persisted snapshot
object, not from a live provider call.

**Axis 2 — a competitor's weekly decision state is independent of
checkpoint progression, and is per (season_competitor, week), not
per-week.** A Pounce is explicitly allowed to fire Tuesday, long before
any game's MID or FINAL checkpoint (constitution §59, §73) — so
"decisions locked" can never be a single week-wide gate that Forecast Lab
checkpoints must pass through first. This state is **derived**, not
stored as its own column — computed on read from the existing
`tickets`/`wagers`/`pass_decisions` rows for that
`(season_competitor_id, week_id)`, the same append-only-audit principle
used everywhere else in this schema (a redundant stored status could
drift out of sync with the rows that are actually authoritative):

```
RESEARCHING                              -- no ticket/pass yet this week
  → TICKET_PENDING (an ISSUED ticket exists, urgency WATCH/LEAN/STRONG/POUNCE)
  → BET_EXECUTED (a wager with execution_status = PLACED exists)         [terminal]
  → TICKET_EXPIRED_RETRY_ALLOWED (ticket EXPIRED/SKIPPED, no PLACED wager yet
     for this competitor+week — a fresh ticket may still be issued)
      → back to TICKET_PENDING, or:
RESEARCHING → PASS_LOCKED (a pass_decisions row exists)                  [terminal]
```

`SeasonService`/`CommissionerService` enforce the two terminal states as
mutually exclusive and single-use per `(season_competitor_id, week_id)`
(constitution §12: at most one official wager per competitor per week) —
this is exactly what `DuplicateWeeklyDecision` guards in the Phase 1
implementation. Nothing about reaching `BET_EXECUTED` or `PASS_LOCKED`
for one competitor requires or blocks any other competitor's state, and
nothing about it requires any particular Forecast Lab checkpoint to have
run.

**Ticket execution validation.** `record_execution` (the Phase 1
implementation of the `TICKET_PENDING → BET_EXECUTED` transition above) is
the sole authoritative gate for turning a ticket into a real wager — every
hard constraint the ticket carries is enforced there, not left to
whatever recorded the human's execution input: the ticket must still be
`ISSUED` and unexpired; `actual_stake` must be within both the ticket's
`final_allowed_stake` *and* current available bankroll (the two can
diverge if bankroll moved between issuance and execution), at least
`minimum_stake`, and a whole multiple of `stake_increment`; `actual_line`
must not have moved past `acceptable_line_boundary` in the side-aware
direction (worse for OVER means higher, worse for UNDER means lower); and
`actual_price` must be at least as good as `worst_acceptable_price` (the
DATABASE.md §7 rename from "maximum" — American odds aren't ordered by
raw magnitude once sign is involved, e.g. -125 is worse than -120 but
that's not simply "a bigger negative number," so this compares via
decimal/implied-probability odds, `price_is_acceptable()` in
`backend/app/domain/risk.py`, rather than comparing the raw integers or
their magnitudes).

## 4a. Benchmark slate: precommitted, resolved asynchronously per game

Axis 1 above (per-game checkpoint progression) creates a real conflict for
the benchmark slate: constitution §20 requires all three competitors to
forecast the exact same ~10-prop slate, but a Thursday game's OPENING
window can close (T-96h before its kickoff) several days before a Monday
game's OPENING window even *opens*. Waiting for the whole week's slate to
be fully known before forecasting anything would mean missing TNF's
Opening checkpoint entirely — defeating the purpose of a kickoff-relative
Opening window. Two-phase construction resolves this: **commit the slot
plan mechanically before any game's window opens; resolve each slot's
actual market only when that slot's own game reaches OPENING.**

**Phase 1 — commit the plan** (`commit_benchmark_slate_plan(week)`, runs
once, as part of `open_week`, which always happens well before the
week's earliest game reaches T-144h):

1. List the week's games ordered by `kickoff_at`.
2. Allocate `benchmark_slate_size` (10) slots across games by a fixed,
   deterministic rule. **Kickoff order is a placeholder, not the frozen
   rule** — with 10 slots against a 16-game week, "round-robin by
   kickoff, remainder to the earliest games" systematically favors
   Thursday/early-Sunday games and could under-select late-Sunday/Monday
   games every single week. Before Week 1 this needs a rule with no such
   bias: e.g. a deterministic shuffle seeded only from
   `(season_id, week_id)` (schedule identifiers, never odds or forecasts)
   to pick which games get a slot, still resolved async per-game as
   below. Validate the frozen choice in Week 0.
3. Assign each slot a `target_stat_type` (cycling through
   `supported_prop_types`) plus a fixed `fallback_stat_types` priority
   order, so a game missing its target type still has a deterministic
   next choice, decided now rather than when the slot resolves.
4. Persist `benchmark_slate_plans` (one row) and `benchmark_slots` (one
   row per slot, `status = PENDING`, `resolved_market_id = NULL`).

Every decision in this phase depends only on the schedule and the fixed
prop-type list — never on odds, forecasts, or anything that could look
like cherry-picking after the fact.

**Phase 2 — resolve each game's slots at its own OPENING window**
(folded into the existing `capture_checkpoint(game, OPENING)` job, §6):

1. Find this plan's `PENDING` slots where `game_id` = this game.
2. For each, pick the qualifying `prop_market` for
   `(game, target_stat_type)` — canonical market available, two-sided
   priced, non-pushable (RULES.md §6a/§22), sufficient data quality —
   using one fixed tiebreak rule (exact rule TBD/frozen in Week 0, e.g.
   lowest `player_id`); mark the slot `RESOLVED` with `resolved_market_id`.
3. If `target_stat_type` has no qualifying market for this game, try each
   `fallback_stat_types` entry in order; if none qualify, mark the slot
   `UNFILLABLE`.
4. The now-`RESOLVED` slots' markets are included in this game's
   `run_benchmark_forecast(game, OPENING, ...)` call alongside whatever
   else that job does — all three competitors see the identical resolved
   market at the identical Opening snapshot for that slot, they just see
   it on that game's own schedule rather than the whole week's.

**`UNFILLABLE` slots are not reallocated to another game.** A more
elaborate overflow rule (roll a dead slot's budget onto the next
unresolved game) was considered and rejected for V1: it adds a second
layer of "the algorithm decided who gets the extra slot" that itself
invites the appearance of cherry-picking, for a problem this project
already has a standard answer to — report coverage rather than
manufacture completeness (RULES.md §16 does exactly this for the
common-coverage cohort). A week's realized benchmark slate may therefore
land under 10 props; that's reported, not backfilled. Revisit only if
Week 0 testing shows `UNFILLABLE` slots are chronically common enough to
matter.

MID and FINAL never re-run this algorithm — they simply reforecast
whichever market each slot already resolved to, at that game's own
MID/FINAL `checkpoint_run` (Axis 1).

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
timestamp, prompt_version, schema_version, raw structured response,
validated response, and bankroll_at_decision (constitution §106) — this
is written by the orchestrator wrapper around every adapter call, not by
the adapters themselves, so it can never be skipped. For a single-market
call this also includes `evidence_snapshot_id`/`market_snapshot_id`
directly on the `agent_sessions` row; for a batched
`forecast_benchmark(slate, snapshot_ids)` call covering N props, those two
columns stay null and the wrapper instead writes one
`agent_session_evidence_snapshots` row per snapshot in the batch
(DATABASE.md §6) — each resulting `ForecastObservation` still carries its
own `evidence_snapshot_id`, so per-prop lineage never depends on the
batch-level join table.

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
| `capture_checkpoint(game, checkpoint_type)` | that game's kickoff-relative window open, manual or scheduled | `(game_id, checkpoint_type)` — no-ops if `checkpoint_runs` row already `CAPTURED` |
| `poll_market_events(game)` | interval | dedupes via `MarketEvent` fingerprint |
| `run_benchmark_forecast(game, checkpoint, season_competitor)` | after that game's checkpoint capture | `(game_id, checkpoint, season_competitor_id)` |
| `lock_research_settlement(game)` | T+72h after that game completes | `game_id` — refuses to re-lock once `research_locked_at` set (per market) |
| `settle_sportsbook_wager(wager_id)` | manual (human reports result) | `wager_id` |
| `close_week(week)` | every game in the week is research-settlement-locked (or otherwise finalized) | `week_id` |
| `generate_weekly_receipt(week)` | after `close_week` | `week_id` |

A Thursday game's `capture_checkpoint`/`run_benchmark_forecast`/
`lock_research_settlement` cycle runs and completes independently of a
Monday game in the same week — this is the mechanism behind Axis 1 in §4.

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
- `AuditLog` is a thin read-side: given a `week_id` (or `season_competitor_id`,
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
