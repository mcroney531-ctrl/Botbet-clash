# RULES.md — AI Prop Betting League

Season rules, frozen for Week 0 testing. This document is the machine-relevant
translation of `CONSTITUTION.md` (project charter). Where the constitution
gave a "current recommendation," this document commits to a concrete value so
the system can be built and tested. Every value here is versioned via
`SeasonRules.rules_version` and must not change mid-season except for a
recorded emergency amendment (see §14).

## 1. Season basics

| Field | Value |
|---|---|
| `rules_version` | `"2026-w0.1"` |
| `starting_bankroll_cents` | `1500` ($15.00) |
| `competitors` | `openai`, `anthropic`, `google` |
| `sport` | NFL |

Money is **always** integer cents. Never floating point.

## 2. Competitors

Frozen before Week 1. Any change to `model_identifier` or `model_version`
mid-season makes that competitor a new competitor for record-keeping
purposes (constitution §5).

| `competitor_id` | provider | model_identifier (placeholder — freeze before Week 1) |
|---|---|---|
| `openai` | OpenAI | TBD |
| `anthropic` | Anthropic | TBD |
| `google` | Google | TBD |

## 3. Week 0

Week 0 (`week_number = 0`) runs the full weekly lifecycle with **no real
money and no real AI wagers exposed**:

- `is_real_money = false`
- `counts_toward_standings = false`
- `counts_toward_awards = false`

Its acceptance criterion is the MVP flow in constitution §116 (all 25 steps)
run end-to-end on fake/mocked data.

## 4. Canonical sportsbook & research settlement source

Frozen for Week 0 (revisit before Week 1 if better data access is found):

- `canonical_sportsbook = "DRAFTKINGS"` — research baseline for all Forecast
  Lab probability comparisons. Fixed for the season once Week 1 begins.
- `research_settlement_provider = "NFL_OFFICIAL_STATS"` — "official NFL
  statistical record, or a provider explicitly mirroring it."
- `research_settlement_delay_hours = 72` (T+72h after game completion).

If the canonical sportsbook has no valid two-sided quote at a checkpoint
window, that observation is `research_eligible = false`,
`exclusion_reason = CANONICAL_MARKET_UNAVAILABLE`. Never substitute another
book and call it canonical.

## 5. De-vig method

- `devig_method = "PROPORTIONAL_V1"` — frozen for the season.

American odds → raw implied probability:

```
negative odds:  p = |odds| / (|odds| + 100)
positive odds:  p = 100 / (odds + 100)
```

Given `p_over_raw`, `p_under_raw`:

```
p_over  = p_over_raw  / (p_over_raw + p_under_raw)
p_under = p_under_raw / (p_over_raw + p_under_raw)
```

## 6. Supported prop universe

V1 research universe (`stat_type`):

```
passing_yards
passing_touchdowns
rushing_yards
receptions
receiving_yards
```

`anytime_td` is Competition-eligible (real wagers) but excluded from V1
Forecast Lab benchmark construction. Market eligibility is configurable via
`SeasonRules.supported_prop_types`.

## 7. Benchmark slate

- `benchmark_slate_size = 10`
- Constructed mechanically (not hand-picked), favoring: supported prop
  types, canonical market availability, valid two-sided prices, sufficient
  data quality, non-pushable lines, diversity across games and prop
  families (constitution §20–21).
- `batch_methodology`: **to be decided empirically in Week 0** between
  `BATCH_10`, `BATCH_SMALL` (2–3 per call), and `ISOLATED`. Default
  starting hypothesis for the Week 0 harness: `BATCH_SMALL` (3 per call).
  Freeze the winner in `SeasonRules.batch_methodology` before Week 1.

## 8. Pushable lines

Non-pushable (half-point or otherwise non-integer) lines are
`research_eligible = true` candidates for the primary binary research
cohort. Integer lines remain valid **Competition** wagers but are excluded
from the primary research cohort with
`exclusion_reason = RESEARCH_INELIGIBLE_PUSHABLE_LINE`. Eligibility is
determined pregame and never revised based on whether the line actually
pushed.

## 9. Checkpoints (kickoff-relative)

Frozen starting windows, to be validated against real prop availability in
Week 0 and then locked for the season:

| Checkpoint | Window | Target |
|---|---|---|
| `OPENING` | T-144h → T-96h | earliest qualifying snapshot in window |
| `MID` | T-60h → T-36h | snapshot closest to T-48h |
| `FINAL` | T-6h → T-2h | snapshot closest to T-3h |

A market with no observation inside a window simply does not receive that
checkpoint (no back-filling, no relabeling a later snapshot as an earlier
checkpoint).

`OPENING` is the primary/headline research checkpoint. `MID` and `FINAL`
are secondary, answering separate questions (constitution §31–32).

## 10. Kelly / stake sizing

- `kelly_fraction = 0.20` (1/5 Kelly) — Week 0 default, to be validated
  against `0.25` and `0.125` alternatives, then frozen before Week 1.
- `standard_max_bankroll_fraction = 0.20` (20% of **current available**
  bankroll).
- `exceptional_max_bankroll_fraction = 0.30` (30%, Pounce/exceptional only).
- `minimum_stake_cents = 25` ($0.25).
- `stake_increment_cents = 25`.
- Available bankroll excludes any unresolved (pending) exposure — never
  starting, projected, stale, or hypothetical bankroll.

Stake resolution order (constitution §63–65): bankroll → model probability
→ market price → estimated edge → full Kelly reference → fractional Kelly
reference → (confidence, uncertainty considered by the model) → model
requested stake → risk posture → hard caps → final allowed stake. The
system validates/caps; it never overrides the model's qualitative sizing
logic below the cap.

Always persist three numbers per wager: `kelly_reference_stake_cents`,
`model_requested_stake_cents`, `final_allowed_stake_cents`.

## 11. Weekly decision & Pounce

- Each competitor makes **at most one official real-money wager per NFL
  week**, concluding in `BET` or `PASS`.
- `PASS` requires structured reasoning: `best_available_candidate`,
  `estimated_edge`, `confidence`, `uncertainty`, `reason_for_pass`.
- `pounce_limit = 1` active Pounce ticket per competitor per week. An
  expired, unexecuted Pounce may be replaced later in the same week.
- `attribution_confidence_threshold = 0.75` — below this, market-movement
  attribution is `ATTRIBUTION_UNKNOWN` unless manually reviewed.
- Final weekly execution deadline: **to be set during Week 0** after
  observing real ticket → human-execution turnaround
  (`SeasonRules.weekly_decision_deadline`).

## 12. Bankroll & bankruptcy

- All bankroll movement is through `BankrollTransaction` rows
  (`SEASON_START`, `STAKE`, `WIN_RETURN`, `PUSH_RETURN`, `VOID_RETURN`,
  `ADJUSTMENT`). Current bankroll is **derived** (sum of transactions),
  never stored/overwritten directly.
- Bankruptcy: `available_bankroll_cents < minimum_stake_cents` →
  competitor status becomes `BUSTED`. Real-money bankroll is frozen
  permanently for the season. No bailout.
- A busted competitor continues forecasting, watchlisting, and public
  commentary for the season story; an optional post-bust paper account may
  exist but must never merge into real-money standings.

## 13. Independence & information isolation

Until a wager is public, competitors must not see each other's private
watchlists, forecasts, rankings, probabilities, confidence, stake
intentions, or reasoning. Show-layer commentary is one-directional: it may
read Competition/Forecast Lab state, but its output must never be fed back
into another competitor's handicapping prompt. Once a wager is public,
rivals may react socially (Call Your Shot, trash talk), but that reaction
must not enter their own analytical pipeline.

## 14. Rule changes

Once Week 1 begins, consequential rule changes are prohibited except for
genuine system failures. Any emergency amendment must be recorded as a new
`rules_version` with an attached reason and timestamp — never a silent
in-place edit.

## 15. Research exclusion reasons (enum)

```
CANONICAL_MARKET_UNAVAILABLE
TWO_SIDED_PRICE_UNAVAILABLE
RESEARCH_INELIGIBLE_PUSHABLE_LINE
GPT_FORECAST_INVALID
GPT_FORECAST_MISSING
CLAUDE_FORECAST_INVALID
CLAUDE_FORECAST_MISSING
GEMINI_FORECAST_INVALID
GEMINI_FORECAST_MISSING
MARKET_WITHDRAWN
PLAYER_STATUS_INVALIDATED
RESEARCH_OUTCOME_UNAVAILABLE
OTHER
```

## 16. Common-coverage research cohort

A prop/checkpoint enters the headline cross-model cohort only if all of
the following exist: valid forecast from all three competitors, valid
canonical market baseline, valid research outcome, research-eligible line.
Otherwise the row is stored but excluded, with an explicit reason (§15).
No imputation, no carrying old forecasts forward, no book substitution.
Every headline metric reports its coverage denominator (e.g.
`147 / 180`).

## 17. Claim discipline

Research-grade claims (benchmark Brier/log-loss, disagreement, calibration,
updating behavior) may support cautious comparative findings. Descriptive
metrics (real ROI, official W-L, Pounce record, CLV, ~18 wagers/season)
describe what happened and must not be presented as proof of stable skill.
Correlated observations (shared game/player/weather) are not independent;
do not compute naive confidence intervals across them.

## 18. Human execution boundary

The system never automates sportsbook login or wager placement. It
produces an executable ticket; the human records one of `PLACED`,
`MARKET_MOVED`, `UNAVAILABLE`, `MISSED_WINDOW`, `SKIPPED`, plus (if placed)
`sportsbook`, `actual_line`, `actual_price`, `actual_stake`,
`execution_timestamp`. Requested and executed wager fields are always
stored separately, never collapsed.

## 19. Items still open for Week 0 validation

These are committed to a starting value above but are explicitly subject
to revision from Week 0 test results, and must be re-frozen (new
`rules_version`) before Week 1:

- `batch_methodology`
- `kelly_fraction`
- exact checkpoint windows
- `weekly_decision_deadline`
- notification channel(s)
