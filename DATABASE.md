# DATABASE.md — AI Prop Betting League

PostgreSQL schema. Companion to `RULES.md` and `ARCHITECTURE.md`. Types are
given as SQLAlchemy-ish/Postgres types; exact column list will grow as
later phases (real providers, attribution, show layer) land, but the
shapes below are the frozen core from constitution §112 and match the
Phase 1/2 models implemented under `backend/app/db/models/`.

**v2 note:** this revision fixes several issues found in review before
Phase 2 persistence was built on top of v1: research settlement was
modeled per-market instead of per-observation-line (§8), checkpoint
capture was modeled per-week instead of per-game/kickoff (§4, new),
batched AI calls had nowhere to record multiple evidence inputs (§6),
competitors were not season-scoped (§1), and `pass_decisions` had a
uniqueness bug. See each section below for the fix and rationale.

Conventions used throughout:

- All money is `BIGINT` cents. Never `NUMERIC`/`FLOAT` for money.
- All primary keys are `UUID` unless noted.
- All tables have `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Append-only tables have no `updated_at`; mutable config/status tables do.
- "Append-only" means the application layer never issues `UPDATE`/`DELETE`
  against that table — corrections are new rows referencing the old one.
- Every table that is logically "per competitor" is keyed by
  `season_competitor_id`, not by the bare provider identity — see §1.

## 1. Season, rules & competitors

```
seasons
  id                UUID PK
  name              TEXT            e.g. "2026 NFL Season"
  year              INT
  status            TEXT            DRAFT | ACTIVE | COMPLETE
  started_at        TIMESTAMPTZ NULL
  ended_at          TIMESTAMPTZ NULL

season_rules                         -- append-only, versioned
  id                          UUID PK
  season_id                   UUID FK -> seasons
  rules_version                TEXT UNIQUE
  starting_bankroll_cents      BIGINT
  canonical_sportsbook         TEXT
  research_settlement_provider TEXT
  research_settlement_delay_hours INT
  supported_prop_types         TEXT[]
  devig_method                 TEXT
  benchmark_slate_size         INT
  batch_methodology            TEXT
  checkpoint_windows           JSONB   -- {OPENING: {...}, MID: {...}, FINAL: {...}}
  kelly_fraction                NUMERIC(5,4)
  standard_max_bankroll_fraction    NUMERIC(5,4)
  exceptional_max_bankroll_fraction NUMERIC(5,4)
  minimum_stake_cents           BIGINT
  stake_increment_cents         BIGINT
  pounce_limit                  INT
  attribution_confidence_threshold NUMERIC(4,3)
  weekly_decision_deadline_rule JSONB  -- kickoff-relative rule
  competition_requires_nonpushable_line BOOLEAN  -- see RULES.md §6a
  superseded_by                 UUID NULL FK -> season_rules.id
  amendment_reason              TEXT NULL
  effective_from                TIMESTAMPTZ

weeks
  id                UUID PK
  season_id         UUID FK -> seasons
  week_number       INT              0 = Week 0
  is_real_money     BOOLEAN
  counts_toward_standings BOOLEAN
  counts_toward_awards    BOOLEAN
  status            TEXT             aggregate bookkeeping only — see
                                      ARCHITECTURE.md §4; NOT a per-
                                      competitor or per-checkpoint state
  opened_at         TIMESTAMPTZ NULL
  closed_at         TIMESTAMPTZ NULL
  research_locked_at TIMESTAMPTZ NULL
  UNIQUE (season_id, week_number)

competitors                           -- cross-season identity only
  id                TEXT PK          e.g. 'openai' | 'anthropic' | 'google'
  provider          TEXT
  display_name      TEXT

season_competitors                    -- one row per competitor per season;
                                       -- everything competitive points here
  id                UUID PK
  season_id         UUID FK -> seasons
  competitor_id     TEXT FK -> competitors
  model_identifier  TEXT
  model_version     TEXT
  provider_metadata JSONB
  status            TEXT             ACTIVE | BUSTED
  frozen_at         TIMESTAMPTZ      when roster locked before Week 1
  UNIQUE (season_id, competitor_id)
```

**Why the split (fixes v1 bug):** v1 had a single global `competitors` row
carrying `model_version`/`status` directly, with every competitive table
(bankroll, tickets, wagers, forecasts, events...) pointing at
`competitors.id` — a bare string like `'openai'`. That's fine for one
season, but `SUM(amount_cents) WHERE season_competitor_id = ...` for
bankroll (§8) or a Brier average for a competitor (RULES.md §33) would
silently blend two different model versions across two different seasons
the moment Season 2 starts, since nothing in those tables was season-
scoped. `competitors` now holds only the cross-season identity
(`'openai'` is always OpenAI, across every season); `season_competitors`
holds the actual competitive instance — model version, status, bankroll
identity — for one season. **Every FK that was `competitor_id TEXT FK ->
competitors` below is now `season_competitor_id UUID FK ->
season_competitors`.**

## 2. Games, players, markets, quotes

```
games
  id                UUID PK
  external_ref      TEXT UNIQUE      provider game id — single-provider
                                      assumption; becomes a
                                      provider-scoped mapping table
                                      (game_id, provider, provider_ref) once
                                      a second sports-data provider is added
  season_id         UUID FK -> seasons
  week_number       INT
  home_team         TEXT
  away_team         TEXT
  kickoff_at        TIMESTAMPTZ
  status            TEXT             SCHEDULED | IN_PROGRESS | FINAL

players
  id                UUID PK
  external_ref      TEXT UNIQUE      same single-provider caveat as above
  name              TEXT
  team              TEXT
  position          TEXT

prop_markets                          -- conceptual market, book-agnostic
  id                UUID PK
  game_id           UUID FK -> games
  player_id         UUID FK -> players
  stat_type         TEXT             passing_yards | receptions | ...
  is_pushable       BOOLEAN          derived from canonical line at research eligibility check time
  created_at        TIMESTAMPTZ

prop_quotes                           -- append-only, immutable per snapshot
  id                UUID PK
  market_id         UUID FK -> prop_markets
  sportsbook        TEXT
  line              NUMERIC(6,2)
  over_price        INT              American odds
  under_price       INT
  retrieved_at      TIMESTAMPTZ
  INDEX (market_id, sportsbook, retrieved_at)

market_snapshots                      -- frozen consensus/canonical read at a point in time
  id                        UUID PK
  market_id                 UUID FK -> prop_markets
  taken_at                  TIMESTAMPTZ
  canonical_sportsbook      TEXT
  canonical_line            NUMERIC(6,2) NULL
  canonical_over_price      INT NULL
  canonical_under_price     INT NULL
  canonical_over_probability  NUMERIC(6,5) NULL   -- de-vigged
  canonical_under_probability NUMERIC(6,5) NULL
  devig_method              TEXT
  same_line_consensus_over_probability NUMERIC(6,5) NULL
  market_median_line        NUMERIC(6,2) NULL
  market_min_line           NUMERIC(6,2) NULL
  market_max_line           NUMERIC(6,2) NULL
  number_of_books           INT
  is_valid_canonical_baseline BOOLEAN   -- per constitution §19 checklist
```

## 3. News / injuries / weather / events

```
news_items
  id                UUID PK
  event_timestamp    TIMESTAMPTZ
  retrieved_timestamp TIMESTAMPTZ
  source            TEXT
  event_type        TEXT
  affected_entities JSONB            -- player_ids/team refs
  summary           TEXT

injury_events
  id                UUID PK
  player_id         UUID FK -> players
  designation       TEXT             OUT | DOUBTFUL | QUESTIONABLE | ACTIVE ...
  event_timestamp    TIMESTAMPTZ
  retrieved_timestamp TIMESTAMPTZ
  source            TEXT

weather_snapshots
  id                UUID PK
  game_id           UUID FK -> games
  retrieved_at      TIMESTAMPTZ
  conditions        JSONB

market_events                         -- MarketEventDetector output
  id                UUID PK
  market_id         UUID FK -> prop_markets
  event_type        TEXT             LINE_MOVE | PRICE_MOVE | INJURY_CHANGE |
                                      INACTIVE | STARTER_ANNOUNCED |
                                      ROSTER_MOVE | WEATHER_THRESHOLD |
                                      MARKET_REOPENED | MARKET_WITHDRAWN
  detected_at       TIMESTAMPTZ
  payload           JSONB
```

## 4. Checkpoint runs (kickoff-relative, per game)

```
checkpoint_runs                       -- the actual capture unit for
                                       -- OPENING/MID/FINAL — one per game,
                                       -- not one per week
  id                UUID PK
  game_id           UUID FK -> games
  checkpoint_type   TEXT             OPENING | MID | FINAL
  window_start      TIMESTAMPTZ      derived from game.kickoff_at + SeasonRules.checkpoint_windows
  window_end        TIMESTAMPTZ
  target_time       TIMESTAMPTZ      e.g. kickoff - 48h for MID
  status            TEXT             PENDING | CAPTURED | MISSED
  captured_at       TIMESTAMPTZ NULL
  UNIQUE (game_id, checkpoint_type)
```

**Why this table exists (fixes v1 bug):** v1's background-job table had
`capture_checkpoint(week, checkpoint_type)` keyed by `(week_id,
checkpoint_type)` — a single MID capture for the whole week. RULES.md §9
already says checkpoints are kickoff-relative per game, and a week
routinely has Thursday, Sunday-early, Sunday-late, Sunday-night, and
Monday games with kickoffs up to 4+ days apart — they cannot share one
MID window. `checkpoint_runs` is per **game** (not per market): every
prop tied to a game shares that game's kickoff, so one capture per
`(game_id, checkpoint_type)` triggers evidence-snapshot capture for every
`prop_market` in that game at once. A market that doesn't exist yet when
its game's window opens simply gets no `evidence_snapshot` for that
checkpoint (RULES.md §9's missing-checkpoint rule), even though the
`checkpoint_run` itself completed for the game.

## 5. Evidence & forecasts

```
evidence_snapshots                    -- immutable
  id                UUID PK
  market_id         UUID FK -> prop_markets
  checkpoint_run_id UUID NULL FK -> checkpoint_runs   -- NULL for event-driven/open-market snapshots
  checkpoint_type   TEXT NULL         OPENING | MID | FINAL | NULL (event-driven/open-market)
  generated_at      TIMESTAMPTZ
  payload           JSONB             -- game/player/recent_stats/team_context/
                                       -- injuries/news_events/weather/
                                       -- canonical_market/market_context/line_history
  market_snapshot_id UUID FK -> market_snapshots

forecast_observations                 -- append-only; revisions are new rows
  id                            UUID PK
  season_competitor_id          UUID FK -> season_competitors
  market_id                     UUID FK -> prop_markets
  source_type                   TEXT    BENCHMARK | OPEN_MARKET
  checkpoint_type               TEXT NULL   OPENING | MID | FINAL | NULL
  timestamp                     TIMESTAMPTZ
  model_probability_over        NUMERIC(6,5)
  canonical_market_probability_over NUMERIC(6,5) NULL
  same_line_consensus_probability_over NUMERIC(6,5) NULL
  canonical_line                NUMERIC(6,2) NULL
  canonical_over_price          INT NULL
  canonical_under_price         INT NULL
  market_median_line            NUMERIC(6,2) NULL
  probability_disagreement      NUMERIC(6,5) NULL   -- model - canonical
  confidence                    NUMERIC(4,2)         -- 1.0-10.0
  uncertainty                   TEXT                 LOW | MEDIUM | HIGH
  evidence_snapshot_id          UUID FK -> evidence_snapshots
  revision_parent_id            UUID NULL FK -> forecast_observations.id
  revision_reason               TEXT NULL   SCHEDULED_CHECKPOINT | NEWS_EVENT |
                                             INJURY_EVENT | LINE_MOVE |
                                             WEATHER_CHANGE | DEPTH_CHART_CHANGE |
                                             MODEL_REVIEW | OTHER
  research_eligible             BOOLEAN
  exclusion_reason              TEXT NULL   -- enum, RULES.md §15
  agent_session_id              UUID FK -> agent_sessions

benchmark_slates
  id                UUID PK
  week_id           UUID FK -> weeks
  checkpoint_type   TEXT               currently only OPENING builds a slate;
                                        MID/FINAL reforecast the same markets
  constructed_at    TIMESTAMPTZ
  selection_method  TEXT

benchmark_slate_entries
  id                UUID PK
  slate_id          UUID FK -> benchmark_slates
  market_id         UUID FK -> prop_markets
```

Each `forecast_observation.canonical_line` is copied at the moment the
forecast was made and never updated afterward. This is what lets Opening
(line 74.5) and Final (line 78.5, if the book moved it) each be graded
independently against the *same* eventual stat value without needing a
single "the" outcome for the market — see §8.

## 6. AI orchestration / reproducibility

```
agent_sessions                        -- one row per consequential AI call
  id                UUID PK
  season_competitor_id UUID FK -> season_competitors
  provider          TEXT
  model_identifier  TEXT
  call_type         TEXT     BENCHMARK_FORECASTING | OPEN_MARKET_RESEARCH |
                              MARKET_EVENT_REVIEW | STAKE_SIZING |
                              FINAL_DECISION | POSTMORTEM |
                              PUBLIC_COMMENTARY | TRASH_TALK
  prompt_version    TEXT
  schema_version    TEXT
  evidence_snapshot_id UUID NULL FK -> evidence_snapshots   -- single-market calls only, see below
  market_snapshot_id   UUID NULL FK -> market_snapshots     -- single-market calls only
  raw_response      JSONB
  validated_response JSONB NULL
  is_valid          BOOLEAN
  retry_count       INT
  bankroll_at_decision_cents  BIGINT
  timestamp         TIMESTAMPTZ

agent_session_evidence_snapshots      -- multi-snapshot inputs for a
                                       -- batched call (constitution §52)
  agent_session_id     UUID FK -> agent_sessions
  evidence_snapshot_id UUID FK -> evidence_snapshots
  market_id            UUID FK -> prop_markets
  PRIMARY KEY (agent_session_id, evidence_snapshot_id)

watchlists
  id                UUID PK
  season_competitor_id UUID FK -> season_competitors
  week_id           UUID FK -> weeks
  agent_session_id  UUID FK -> agent_sessions
  created_at        TIMESTAMPTZ

watchlist_entries
  id                UUID PK
  watchlist_id      UUID FK -> watchlists
  market_id         UUID FK -> prop_markets
  forecast_observation_id UUID NULL FK -> forecast_observations
  status            TEXT      WATCHED | DROPPED
```

**Batching fix:** `agent_sessions.evidence_snapshot_id` /
`market_snapshot_id` stay as plain nullable FKs for call types that are
inherently about one market at a time (`STAKE_SIZING`, `FINAL_DECISION`,
`MARKET_EVENT_REVIEW`). A `BENCHMARK_FORECASTING` call that batches N
props in one request leaves those two columns null and instead gets N
rows in `agent_session_evidence_snapshots` — one per market in the batch.
Each resulting `forecast_observations` row still carries its own
authoritative `evidence_snapshot_id`, so "what did GPT see for prop #7 of
this batch" is always answerable from the observation row alone; the join
table exists so "what was the full input to this one API call" (needed
to audit cross-prop anchoring per constitution §52) is also answerable.
A dedicated `ForecastBatch`/`SnapshotBundle` entity was considered and
rejected as unnecessary — the join table captures the same information
with one fewer concept, since `agent_sessions` already carries
`call_type` and `timestamp` for grouping.

## 7. Risk, tickets, wagers

```
stake_recommendations
  id                        UUID PK
  season_competitor_id      UUID FK -> season_competitors
  market_id                 UUID FK -> prop_markets
  forecast_observation_id   UUID FK -> forecast_observations   -- the exact forecast this sizing is based on
  agent_session_id          UUID FK -> agent_sessions
  bankroll_at_decision_cents BIGINT
  model_probability_over    NUMERIC(6,5)
  canonical_market_probability_over NUMERIC(6,5)
  estimated_edge            NUMERIC(6,5)
  kelly_fraction_used       NUMERIC(5,4)
  kelly_reference_stake_cents  BIGINT
  model_requested_stake_cents  BIGINT
  risk_posture              TEXT     CONSERVATIVE | STANDARD | AGGRESSIVE
  final_allowed_stake_cents BIGINT
  created_at                TIMESTAMPTZ

tickets                               -- executable ticket (Pounce or standard)
  id                        UUID PK
  season_competitor_id      UUID FK -> season_competitors
  week_id                   UUID FK -> weeks
  market_id                 UUID FK -> prop_markets
  stake_recommendation_id   UUID FK -> stake_recommendations
  urgency                   TEXT     WATCH | LEAN | STRONG | POUNCE
  side                      TEXT     OVER | UNDER
  observed_line             NUMERIC(6,2)
  observed_price            INT                                -- the price at `side`, at issuance
  market_snapshot_id        UUID FK -> market_snapshots         -- exact snapshot the line/price came from
  acceptable_line_boundary  NUMERIC(6,2) NULL
  maximum_acceptable_price  INT NULL
  why_market                TEXT
  why_side                  TEXT
  why_price                 TEXT
  why_now                   TEXT
  primary_risk              TEXT
  valid_until               TIMESTAMPTZ NULL
  status                    TEXT     ISSUED | EXPIRED | EXECUTED | SKIPPED
  created_at                TIMESTAMPTZ

wagers                                -- one per executed ticket
  id                        UUID PK
  ticket_id                 UUID FK -> tickets UNIQUE
  season_competitor_id      UUID FK -> season_competitors
  week_id                   UUID FK -> weeks
  market_id                 UUID FK -> prop_markets
  requested_line            NUMERIC(6,2)
  requested_max_price       INT NULL
  requested_stake_cents     BIGINT
  execution_status          TEXT     PLACED | MARKET_MOVED | UNAVAILABLE |
                                      MISSED_WINDOW | SKIPPED
  sportsbook                TEXT NULL
  actual_line               NUMERIC(6,2) NULL
  actual_price              INT NULL
  actual_stake_cents        BIGINT NULL
  execution_timestamp       TIMESTAMPTZ NULL
  bankroll_at_execution_cents BIGINT NULL
  -- CLV tracking
  line_first_identified     NUMERIC(6,2) NULL
  line_at_pounce            NUMERIC(6,2) NULL
  line_at_lock              NUMERIC(6,2) NULL
  closing_line              NUMERIC(6,2) NULL
  price_first_identified    INT NULL
  price_at_pounce           INT NULL
  closing_price             INT NULL

pass_decisions                        -- structured PASS record
  id                        UUID PK
  season_competitor_id      UUID FK -> season_competitors
  week_id                   UUID FK -> weeks
  best_available_candidate_market_id UUID NULL FK -> prop_markets
  estimated_edge            NUMERIC(6,5) NULL
  confidence                NUMERIC(4,2) NULL
  uncertainty               TEXT NULL
  reason_for_pass           TEXT
  agent_session_id          UUID FK -> agent_sessions
  created_at                TIMESTAMPTZ
  UNIQUE (season_competitor_id, week_id)
```

Two fixes here:

- **`pass_decisions` uniqueness bug**: v1 had `week_id ... UNIQUE`, which
  means only one competitor total could ever PASS in a given week — a
  straightforward bug, not a design choice. Corrected to
  `UNIQUE (season_competitor_id, week_id)`, matching every other
  one-decision-per-competitor-per-week table here. (The Phase 1 in-memory
  implementation was never affected — it already keyed passes by a
  `(competitor_id, week_id)` tuple — this was a schema-doc-only bug.)
- **Reproducibility**: `stake_recommendations.forecast_observation_id` and
  `tickets.{observed_price, market_snapshot_id}` close the gap where a
  final allowed stake could be reconstructed in dollars but not tied back
  to the exact forecast and exact quoted price that produced it
  (constitution §106).

**Integer-line / three-outcome markets (V1 scope decision):** the
probability model everywhere above is binary
(`model_probability_over`/`side: OVER | UNDER`). A pushable integer line
(e.g. 75 receiving yards, exactly) is really a three-outcome market
(OVER/PUSH/UNDER), and Kelly/EV sizing against a two-outcome model would
misprice it. Rather than add ternary probability modeling for Week 0,
**RULES.md now requires official Competition wagers to use the same
non-pushable-line eligibility as Forecast Lab** (RULES.md §6a) —
`anytime_td` is dropped from V1 Competition scope entirely for the same
reason (no numeric line at all, so `tickets.observed_line` and the whole
OVER/UNDER model don't apply). Both may return in a later phase behind a
proper three-outcome pricing model.

## 8. Settlement & ledger

```
settlements                           -- sportsbook-side settlement of a wager
  id                        UUID PK
  wager_id                  UUID FK -> wagers UNIQUE
  sportsbook_result         TEXT     WIN | LOSS | PUSH | VOID
  sportsbook_payout_cents   BIGINT
  sportsbook_settled_at     TIMESTAMPTZ

research_settlements                  -- Forecast Lab-side settlement of a
                                       -- MARKET's final stat — NOT an outcome
  id                        UUID PK
  market_id                 UUID FK -> prop_markets UNIQUE
  research_stat_value_at_lock NUMERIC(10,2)
  research_locked_at        TIMESTAMPTZ
  later_corrected_stat      NUMERIC(10,2) NULL
  correction_timestamp      TIMESTAMPTZ NULL

bankroll_transactions                 -- append-only; balance is SUM() over these
  id                        UUID PK
  season_competitor_id      UUID FK -> season_competitors
  week_id                   UUID NULL FK -> weeks
  wager_id                  UUID NULL FK -> wagers
  type                      TEXT     SEASON_START | STAKE | WIN_RETURN |
                                      PUSH_RETURN | VOID_RETURN | ADJUSTMENT
  amount_cents              BIGINT   signed; debit negative, credit positive
  reason                    TEXT NULL   required for ADJUSTMENT
  created_at                TIMESTAMPTZ
  INDEX (season_competitor_id, created_at)
```

**Research settlement fix (was a hard blocker):** v1 stored one
`research_outcome_at_lock: OVER | UNDER` per **market**. But `OPENING`
and `FINAL` can legitimately be forecast against different lines for the
same market (a book can move 74.5 → 78.5 between Tuesday and Sunday) —
whether a given checkpoint's forecast resolved OVER or UNDER depends on
*that checkpoint's own line*, not on the market as a whole. A player
finishing at 76 resolves the 74.5 Opening forecast OVER and the 78.5
Final forecast UNDER simultaneously; there is no single correct
`market`-level outcome to store.

`research_settlements` now stores only the raw final stat value (e.g.
`76`) once per market, locked at `T + research_settlement_delay_hours`
exactly as before. The binary outcome used in Brier/log-loss scoring
(RULES.md §34) is **derived at scoring time**, per `forecast_observation`,
by comparing `research_settlements.research_stat_value_at_lock` against
that specific observation's own `canonical_line`:

```
outcome_for(observation) =
    OVER  if research_stat_value_at_lock > observation.canonical_line
    UNDER if research_stat_value_at_lock < observation.canonical_line
    (a stat exactly equal to canonical_line can only happen for a
     pushable line, which is already research_eligible = false)
```

A later stat correction only ever needs to replace the one value
(`later_corrected_stat`) — every observation's derived outcome is
recomputed from it automatically, so there's no separate
`later_corrected_outcome` to keep in sync.

`available_bankroll_cents(season_competitor_id)` = `SUM(amount_cents)`
over all `bankroll_transactions` for that season-competitor. A `STAKE`
row is written as a negative amount **at execution time**, so cash placed
at risk is removed from the derived balance immediately; it is added back
(win/push/void return, or nothing on a loss) only when the matching
settlement row is written. This is exactly the "exclude unresolved
exposure" rule in RULES.md §12 — there is no separate subtraction step,
because the ledger itself never counts pending stakes as available. Since
the key is now `season_competitor_id` rather than a bare provider string,
this SUM can never blend two seasons' transactions together.

## 9. Attribution

```
attribution_reviews
  id                        UUID PK
  market_event_id           UUID NULL FK -> market_events
  wager_id                  UUID NULL FK -> wagers
  forecast_observation_id   UUID NULL FK -> forecast_observations
  judge_type                TEXT      RULES_ENGINE | MODEL | HUMAN
  judge_model               TEXT NULL
  judge_version             TEXT
  classification            TEXT      NO_NEW_INFO | PUBLIC_INFO_ALREADY_AVAILABLE |
                                       NEW_INFO_AFTER_FORECAST | ATTRIBUTION_UNKNOWN
  classification_confidence NUMERIC(4,3)
  evidence_event_ids        UUID[]
  attribution_note          TEXT
  review_status              TEXT     AUTO | QUEUED_FOR_REVIEW | HUMAN_REVIEWED
  created_at                TIMESTAMPTZ
```

## 10. Show layer

```
call_your_shots
  id                        UUID PK
  challenger_season_competitor_id  UUID FK -> season_competitors
  target_season_competitor_id      UUID FK -> season_competitors
  target_wager_id           UUID FK -> wagers
  prediction                TEXT
  confidence                NUMERIC(4,2)
  result                    TEXT NULL   HIT | MISS | PENDING
  created_at                TIMESTAMPTZ

weekly_receipts
  id                        UUID PK
  week_id                   UUID FK -> weeks UNIQUE
  payload                   JSONB    -- wagers/passes/results/awards/etc, per constitution §97
  generated_at               TIMESTAMPTZ

awards
  id                        UUID PK
  week_id                   UUID NULL FK -> weeks   -- NULL = season award
  season_competitor_id      UUID FK -> season_competitors
  award_type                TEXT
  detail                    JSONB
  granted_at                TIMESTAMPTZ

trash_talk_messages
  id                        UUID PK
  season_competitor_id      UUID FK -> season_competitors
  week_id                   UUID NULL FK -> weeks
  agent_session_id          UUID FK -> agent_sessions
  content                   TEXT
  created_at                TIMESTAMPTZ
```

## 11. Events

```
competition_events                    -- append-only, drives Show + audit
  id                        UUID PK
  timestamp                 TIMESTAMPTZ
  season_id                 UUID FK -> seasons
  week_id                   UUID NULL FK -> weeks
  season_competitor_id      UUID NULL FK -> season_competitors
  event_type                TEXT      -- enum, ARCHITECTURE.md §7 / constitution §99
  payload                   JSONB
  INDEX (season_id, timestamp)
  INDEX (week_id, timestamp)
  INDEX (season_competitor_id, timestamp)
```

## 12. Normalization note

Per constitution §112: this is intentionally not fully normalized yet
(e.g. `evidence_snapshots.payload` and `competition_events.payload` are
JSONB rather than fully relational). Do not decompose these into further
tables until a complete mocked NFL week (Phase 1–4) proves what shapes are
actually needed. Over-normalizing before that point risks migrations
chasing the wrong schema. `checkpoint_runs` (§4) and
`agent_session_evidence_snapshots` (§6) are deliberate exceptions to that
general caution — they aren't speculative normalization, they're fixes
for cases where the flatter v1 shape was provably wrong (couldn't
represent kickoff-relative checkpoints or batched calls at all, not just
"less normalized").
