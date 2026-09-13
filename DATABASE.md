# DATABASE.md — AI Prop Betting League

PostgreSQL schema. Companion to `RULES.md` and `ARCHITECTURE.md`. Types are
given as SQLAlchemy-ish/Postgres types; exact column list will grow as
later phases (real providers, attribution, show layer) land, but the
shapes below are the frozen core from constitution §112 and match the
Phase 1/2 models implemented under `backend/app/db/models/`.

Conventions used throughout:

- All money is `BIGINT` cents. Never `NUMERIC`/`FLOAT` for money.
- All primary keys are `UUID` unless noted.
- All tables have `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Append-only tables have no `updated_at`; mutable config/status tables do.
- "Append-only" means the application layer never issues `UPDATE`/`DELETE`
  against that table — corrections are new rows referencing the old one.

## 1. Season & rules

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
  status            TEXT             see ARCHITECTURE.md state machine
  opened_at         TIMESTAMPTZ NULL
  closed_at         TIMESTAMPTZ NULL
  research_locked_at TIMESTAMPTZ NULL
  UNIQUE (season_id, week_number)

competitors
  id                TEXT PK          e.g. 'openai' | 'anthropic' | 'google'
  provider          TEXT
  model_identifier  TEXT
  model_version     TEXT
  provider_metadata JSONB
  status            TEXT             ACTIVE | BUSTED
  frozen_at         TIMESTAMPTZ      when roster locked before Week 1
```

## 2. Games, players, markets, quotes

```
games
  id                UUID PK
  external_ref      TEXT UNIQUE      provider game id
  season_id         UUID FK -> seasons
  week_number       INT
  home_team         TEXT
  away_team         TEXT
  kickoff_at        TIMESTAMPTZ
  status            TEXT             SCHEDULED | IN_PROGRESS | FINAL

players
  id                UUID PK
  external_ref      TEXT UNIQUE
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

## 4. Evidence & forecasts

```
evidence_snapshots                    -- immutable
  id                UUID PK
  market_id         UUID FK -> prop_markets
  checkpoint_type   TEXT NULL         OPENING | MID | FINAL | NULL (event-driven/open-market)
  generated_at      TIMESTAMPTZ
  payload           JSONB             -- game/player/recent_stats/team_context/
                                       -- injuries/news_events/weather/
                                       -- canonical_market/market_context/line_history
  market_snapshot_id UUID FK -> market_snapshots

forecast_observations                 -- append-only; revisions are new rows
  id                            UUID PK
  competitor_id                 TEXT FK -> competitors
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

## 5. AI orchestration / reproducibility

```
agent_sessions                        -- one row per consequential AI call
  id                UUID PK
  competitor_id     TEXT FK -> competitors
  provider          TEXT
  model_identifier  TEXT
  call_type         TEXT     BENCHMARK_FORECASTING | OPEN_MARKET_RESEARCH |
                              MARKET_EVENT_REVIEW | STAKE_SIZING |
                              FINAL_DECISION | POSTMORTEM |
                              PUBLIC_COMMENTARY | TRASH_TALK
  prompt_version    TEXT
  schema_version    TEXT
  evidence_snapshot_id UUID NULL FK -> evidence_snapshots
  market_snapshot_id   UUID NULL FK -> market_snapshots
  raw_response      JSONB
  validated_response JSONB NULL
  is_valid          BOOLEAN
  retry_count       INT
  bankroll_at_decision_cents  BIGINT
  timestamp         TIMESTAMPTZ

watchlists
  id                UUID PK
  competitor_id     TEXT FK -> competitors
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

## 6. Risk, tickets, wagers

```
stake_recommendations
  id                        UUID PK
  competitor_id             TEXT FK -> competitors
  market_id                 UUID FK -> prop_markets
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
  competitor_id             TEXT FK -> competitors
  week_id                   UUID FK -> weeks
  market_id                 UUID FK -> prop_markets
  stake_recommendation_id   UUID FK -> stake_recommendations
  urgency                   TEXT     WATCH | LEAN | STRONG | POUNCE
  side                      TEXT     OVER | UNDER
  observed_line             NUMERIC(6,2)
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
  competitor_id             TEXT FK -> competitors
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
  competitor_id             TEXT FK -> competitors
  week_id                   UUID FK -> weeks UNIQUE
  best_available_candidate_market_id UUID NULL FK -> prop_markets
  estimated_edge            NUMERIC(6,5) NULL
  confidence                NUMERIC(4,2) NULL
  uncertainty               TEXT NULL
  reason_for_pass           TEXT
  agent_session_id          UUID FK -> agent_sessions
  created_at                TIMESTAMPTZ
```

## 7. Settlement & ledger

```
settlements                           -- sportsbook-side settlement of a wager
  id                        UUID PK
  wager_id                  UUID FK -> wagers UNIQUE
  sportsbook_result         TEXT     WIN | LOSS | PUSH | VOID
  sportsbook_payout_cents   BIGINT
  sportsbook_settled_at     TIMESTAMPTZ

research_settlements                  -- Forecast Lab-side settlement of a market/checkpoint
  id                        UUID PK
  market_id                 UUID FK -> prop_markets
  research_stat_value_at_lock NUMERIC(10,2)
  research_outcome_at_lock  TEXT      OVER | UNDER
  research_locked_at        TIMESTAMPTZ
  later_corrected_stat      NUMERIC(10,2) NULL
  later_corrected_outcome   TEXT NULL
  correction_timestamp      TIMESTAMPTZ NULL

bankroll_transactions                 -- append-only; balance is SUM() over these
  id                        UUID PK
  competitor_id             TEXT FK -> competitors
  week_id                   UUID NULL FK -> weeks
  wager_id                  UUID NULL FK -> wagers
  type                      TEXT     SEASON_START | STAKE | WIN_RETURN |
                                      PUSH_RETURN | VOID_RETURN | ADJUSTMENT
  amount_cents              BIGINT   signed; debit negative, credit positive
  reason                    TEXT NULL   required for ADJUSTMENT
  created_at                TIMESTAMPTZ
  INDEX (competitor_id, created_at)
```

`available_bankroll_cents(competitor_id)` = `SUM(amount_cents)` over all
`bankroll_transactions` for that competitor. A `STAKE` row is written as a
negative amount **at execution time**, so cash placed at risk is removed
from the derived balance immediately; it is added back (win/push/void
return, or nothing on a loss) only when the matching settlement row is
written. This is exactly the "exclude unresolved exposure" rule in
RULES.md §12 — there is no separate subtraction step, because the ledger
itself never counts pending stakes as available.

## 8. Attribution

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

## 9. Show layer

```
call_your_shots
  id                        UUID PK
  challenger_competitor_id  TEXT FK -> competitors
  target_competitor_id      TEXT FK -> competitors
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
  competitor_id             TEXT FK -> competitors
  award_type                TEXT
  detail                    JSONB
  granted_at                TIMESTAMPTZ

trash_talk_messages
  id                        UUID PK
  competitor_id             TEXT FK -> competitors
  week_id                   UUID NULL FK -> weeks
  agent_session_id          UUID FK -> agent_sessions
  content                   TEXT
  created_at                TIMESTAMPTZ
```

## 10. Events

```
competition_events                    -- append-only, drives Show + audit
  id                        UUID PK
  timestamp                 TIMESTAMPTZ
  season_id                 UUID FK -> seasons
  week_id                   UUID NULL FK -> weeks
  competitor_id             TEXT NULL FK -> competitors
  event_type                TEXT      -- enum, ARCHITECTURE.md §7 / constitution §99
  payload                   JSONB
  INDEX (season_id, timestamp)
  INDEX (week_id, timestamp)
  INDEX (competitor_id, timestamp)
```

## 11. Normalization note

Per constitution §112: this is intentionally not fully normalized yet
(e.g. `evidence_snapshots.payload` and `competition_events.payload` are
JSONB rather than fully relational). Do not decompose these into further
tables until a complete mocked NFL week (Phase 1–4) proves what shapes are
actually needed. Over-normalizing before that point risks migrations
chasing the wrong schema.
