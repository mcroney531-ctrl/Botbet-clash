AI Prop Betting League

Project Constitution v3.0

---

1. Project charter

Build a season-long NFL player-prop competition between three AI competitors:

- OpenAI / GPT
- Anthropic / Claude
- Google / Gemini

Each competitor begins the season with a $15 real-money bankroll.

Throughout each NFL week, each competitor independently evaluates player-prop markets, monitors changing information, maintains private opinions, estimates probabilities and market edge, decides whether an opportunity warrants action, determines how much bankroll to risk, and ultimately makes one official weekly decision:

BET or PASS.

The human user manually places all real wagers on the models' behalf.

The project is intentionally both an experiment and entertainment:

70% serious handicapping / forecasting / bankroll-management competition

30% sports entertainment / presentation

The competitive logic must always take precedence over entertainment.

The models are not instructed to entertain the user through their betting decisions.

They are trying to:

«Maximize expected end-of-season bankroll while minimizing risk of ruin.»

The entertainment system observes those real decisions and turns them into a show.

---

2. The three-layer model

The project consists of three conceptually separate systems.

A. Forecast Lab

The research layer.

Its job is to answer:

«Do LLM forecasts contain useful information beyond what the betting market already knows?»

It contains:

- standardized benchmark props
- model probability forecasts
- canonical market baselines
- market disagreement measurements
- fixed forecasting checkpoints
- forecast revisions
- news/event attribution
- Brier and log-loss scoring
- market-vs-model comparisons
- research-grade observations

No real money is required for Forecast Lab observations.

---

B. Competition

The real bankroll game.

Its job is to answer:

«Given access to the same information environment, which model makes the best actual wagering decisions?»

It contains:

- private watchlists
- open-market discovery
- BET/PASS
- confidence
- uncertainty
- expected edge
- stake sizing
- Pounce alerts
- execution timing
- bankroll
- real wagering results
- CLV
- bankruptcy

Each competitor gets at most one official real-money wager per NFL week.

---

C. Show

The entertainment layer.

It contains:

- 3D arena
- avatars
- competitor stations
- floating pick displays
- trash talk
- rivalries
- streaks
- reactions
- weekly awards
- Call Your Shot
- Unanimous Pick alerts
- season storylines
- visual bankroll state

The Show layer may observe Competition and Forecast Lab state.

It must never alter handicapping logic.

---

3. Core competitive objective

Every competitor receives the same underlying objective:

«Maximize expected end-of-season bankroll while managing uncertainty and minimizing unnecessary risk of ruin.»

Models should not optimize for:

- number of wins
- number of wagers
- exciting picks
- agreeing or disagreeing with another model
- generating Pounce alerts
- appearing confident
- entertaining the user
- making comeback bets
- preserving an arena persona
- leaderboard optics
- trash-talk opportunities

A model may PASS when it believes available opportunities do not justify risking bankroll.

A rational PASS is a valid competitive action.

---

4. Competitor independence

Before an official wager becomes public, competitors should not have access to each other's:

- private watchlists
- private forecasts
- candidate rankings
- probability estimates
- confidence levels
- stake intentions
- private reasoning
- Pounce consideration

This prevents model herding.

All three competitors operate independently against the same underlying information environment.

The Show layer may display selected public-facing commentary to the human user, but that commentary should not be fed into rival handicapping prompts.

Once a wager becomes officially public, rivals may react socially to it.

That reaction still must not contaminate their own analytical process.

---

5. Model versions

The season roster should be frozen before Week 1.

For each competitor store:

provider
model_identifier
model_version
provider_metadata

Every model call must record the exact model used.

Do not intentionally upgrade one competitor halfway through the season unless the rules explicitly permit substitutions.

A materially different model is effectively a different competitor.

---

6. Week 0

There is a formal Week 0 before real bankroll competition begins.

Week 0 carries:

- no real wagers
- no official bankroll results
- no standings
- no season awards

Its purpose is to try to break the system.

Week 0 should test:

- sports-data ingestion
- evidence snapshots
- canonical-market snapshots
- consensus diagnostics
- de-vig calculations
- benchmark-slate creation
- batch vs isolated forecasting
- model output schemas
- fixed checkpoint scheduling
- forecast revisions
- Pounce notifications
- stake calculations
- fake wager execution
- settlement
- event attribution
- weekly receipts
- audit reconstruction

No real bankroll should be exposed until the entire weekly lifecycle works end-to-end.

---

7. High-level architecture

SPORTS DATA PROVIDERS
        ↓
NORMALIZATION
        ↓
IMMUTABLE SNAPSHOT STORE
        ↓
SHARED EVIDENCE ENGINE
        ↓
─────────────────────────────────
│                               │
FORECAST LAB              COMPETITION ENGINE
│                               │
│                         AI ORCHESTRATOR
│                               │
│                         RISK / STAKE ENGINE
│                               │
│                         TICKET ENGINE
│                               │
─────────────────────────────────
                ↓
        HUMAN EXECUTION
                ↓
           SETTLEMENT
                ↓
       ANALYTICS / LEDGER
                ↓
       PRESENTATION API
                ↓
       CONTROL UI / ARENA

Cross-cutting systems:

CommissionerService
NotificationService
CompetitionEventBus
AttributionJudge
AuditLog

The arena never owns betting logic.

---

8. Recommended technical stack

Backend

Python + FastAPI.

Recommended:

- FastAPI
- Pydantic
- SQLAlchemy
- Alembic
- PostgreSQL

Start with a modular monolith.

Do not begin with microservices.

---

Frontend

Next.js + TypeScript.

Initial frontend should be an operational control panel, not the final arena.

Later 3D work can use:

- Three.js
- React Three Fiber
- Drei
- Astra-created 3D assets

---

Realtime communication

Start with Server-Sent Events.

Example:

GET /events/stream

Most realtime communication is server → client, making SSE sufficient initially.

---

9. Commissioner

Create a neutral authority:

CommissionerService

It owns:

- rules enforcement
- season configuration
- weekly state
- deadlines
- eligibility
- bankroll accounting
- stake-cap enforcement
- ticket validation
- settlement
- exceptions
- overrides
- competition integrity
- awards
- historical records

No competitor grades its own performance.

---

10. Season configuration

Create a versioned rules object.

Example:

SeasonRules
- rules_version
- starting_bankroll
- canonical_sportsbook
- supported_prop_types
- devig_method
- benchmark_slate_size
- checkpoint_windows
- kelly_fraction
- standard_max_bankroll_fraction
- exceptional_max_bankroll_fraction
- minimum_stake
- stake_increment
- pounce_limit
- attribution_confidence_threshold
- research_settlement_delay

Once Week 1 begins, consequential rules should not change midseason except for genuine system failures.

Any emergency amendment must be versioned and recorded.

---

11. Starting bankroll

Each competitor begins with:

$15.00

Use integer cents internally:

1500

Never store money using floating-point arithmetic.

---

12. Weekly decision

Each competitor must conclude the week with:

BET

or:

PASS

There is no competitive penalty for passing.

A PASS must still contain structured reasoning:

best_available_candidate
estimated_edge
confidence
uncertainty
reason_for_pass

Example:

«Best candidate showed only a 1.8-point estimated edge with high uncertainty. Preserving bankroll is preferred.»

---

13. Supported prop universe

Recommended V1 research universe:

passing_yards
passing_touchdowns
rushing_yards
receptions
receiving_yards

Other markets may be added later.

Anytime TD props may remain Competition-eligible even if excluded from early Forecast Lab work.

Market eligibility should be configurable.

---

14. Canonical prop identity

Separate the conceptual market from individual sportsbook offers.

Example:

PropMarket
- id
- game_id
- player_id
- stat_type

Sportsbook-specific prices are stored separately.

---

15. Prop quotes

PropQuote
- id
- market_id
- sportsbook
- line
- over_price
- under_price
- retrieved_at

Never overwrite market history.

Line and price snapshots are immutable historical records.

---

16. Canonical sportsbook

Before Week 1, select one sportsbook as the canonical research baseline.

That sportsbook remains fixed throughout the season.

The canonical sportsbook determines the official market probability used in Forecast Lab comparisons.

The actual real wager may be placed elsewhere.

These concepts remain separate:

CANONICAL MARKET
→ research baseline

BROADER MARKET
→ diagnostic context

ACTUAL SPORTSBOOK
→ real execution

If the canonical market is unavailable at a research checkpoint, do not silently substitute another sportsbook.

That observation becomes research-ineligible for that checkpoint.

---

17. Consensus market diagnostics

A secondary market view should also be stored.

However, prices from different prop lines must not be treated as probabilities for the same event.

Therefore:

Same-line consensus

When multiple qualifying sportsbooks offer the same line as the canonical book:

same_line_consensus_probability

may be calculated from their de-vigged prices.

A robust aggregate such as the median is preferred.

Market-line context

Separately store:

market_median_line
market_min_line
market_max_line
number_of_books

Do not average probabilities from books quoting materially different lines and call the result a consensus probability.

---

18. De-vig method

The de-vig method is season-level and frozen before Week 1.

Recommended V1:

PROPORTIONAL_V1

For American odds, first calculate raw implied probabilities.

For negative American odds:

p = |odds| / (|odds| + 100)

For positive American odds:

p = 100 / (odds + 100)

Given raw two-sided probabilities:

p_over_raw
p_under_raw

the de-vigged market probabilities are:

p_over =
p_over_raw / (p_over_raw + p_under_raw)

p_under =
p_under_raw / (p_over_raw + p_under_raw)

Every baseline calculation stores:

devig_method = proportional_v1

Do not change methods midseason.

---

19. Valid canonical baseline

A research-grade canonical baseline requires:

- valid market
- same proposition
- same line
- both sides priced
- canonical sportsbook
- timestamped snapshot
- valid de-vig calculation

If these conditions are not met, the research baseline is unavailable.

Do not manufacture one.

---

20. Forecast Lab benchmark slate

Each NFL week contains a standardized benchmark slate of approximately:

10 props

All three competitors forecast the exact same slate.

This removes selection bias from cross-model comparisons.

The benchmark slate should be constructed mechanically rather than manually cherry-picked.

Selection should favor:

- supported prop types
- canonical market availability
- valid two-sided prices
- sufficient data quality
- half-point / non-pushable lines
- reasonable diversity across games
- reasonable diversity across prop families

Do not intentionally select only interesting or easy-looking markets.

---

21. Benchmark slate diversity

The ten-prop slate should avoid gratuitous concentration.

For example, avoid constructing a slate consisting almost entirely of:

QB passing yards
WR1 receptions
WR1 receiving yards
WR2 receiving yards

from the same offense.

Correlated markets are allowed.

The selection process should simply seek reasonable diversity where possible.

---

22. Pushable lines

V1 research-grade Forecast Lab observations should use non-pushable binary lines whenever possible.

Examples:

74.5 receiving yards
4.5 receptions
1.5 passing TDs

Preferred.

Integer lines such as:

75 receiving yards

remain valid Competition wagers.

However, they are excluded from the primary binary research cohort.

Reason:

A push creates a third outcome and would require a separate probabilistic scoring framework.

Exclusion reason:

RESEARCH_INELIGIBLE_PUSHABLE_LINE

Eligibility is determined before the game.

Do not remove only the observations that happen to push.

---

23. Universal ForecastObservation

Use one observation model for both benchmark and open-market research.

ForecastObservation
- id
- competitor_id
- market_id
- source_type
- checkpoint_type
- timestamp
- model_probability_over
- canonical_market_probability_over
- same_line_consensus_probability_over
- canonical_line
- canonical_over_price
- canonical_under_price
- market_median_line
- probability_disagreement
- confidence
- uncertainty
- evidence_snapshot_id
- revision_parent_id
- revision_reason
- research_eligible
- exclusion_reason

"source_type":

BENCHMARK
OPEN_MARKET

This keeps the data model clean.

---

24. Open-market discovery

Competitors are not limited to the ten benchmark props.

They may independently research additional eligible markets.

Any open-market prop that a model seriously evaluates should generate a normal "ForecastObservation".

That observation should carry:

- model probability
- contemporaneous canonical baseline when available
- confidence
- uncertainty
- evidence snapshot
- timestamp
- estimated edge

The eventual official wager may come from either:

BENCHMARK

or:

OPEN_MARKET

The benchmark exists for experimental cleanliness.

It must not artificially restrict the wagering competition.

---

25. Opportunity capture

Because every serious candidate uses the same observation structure, the system can evaluate whether a model chose well among its own opportunities.

Example:

Candidate A → estimated edge +2.8%
Candidate B → estimated edge +5.1%
Candidate C → estimated edge +10.4%
Candidate D → estimated edge -1.2%

If the model wagers Candidate A, that decision is analytically interesting even if A wins.

This produces a separate concept:

OPPORTUNITY CAPTURE

Possible questions:

- Did the model choose one of its highest-rated opportunities?
- Did it ignore its own strongest signal?
- Did the chosen market have the best risk-adjusted edge?
- Did uncertainty justify passing a nominally larger edge?

This measures selection skill separately from forecasting skill.

---

26. Evidence snapshots

All models receive the same immutable information snapshot at standardized benchmark checkpoints.

Example:

{
  "generated_at": "...",
  "game": {},
  "player": {},
  "recent_stats": [],
  "team_context": {},
  "injuries": [],
  "news_events": [],
  "weather": {},
  "canonical_market": {},
  "market_context": {},
  "line_history": []
}

Every forecast points to the exact snapshot used.

This should allow later reconstruction of:

«What exactly did GPT know when it said 61%?»

---

27. Atomic checkpoint snapshots

For standardized Forecast Lab comparisons:

1. Capture market and evidence state.
2. Freeze the snapshot.
3. Provide that identical snapshot to all three competitors.
4. Store all responses against that snapshot.

Do not allow the market to continue moving independently underneath each model call.

The snapshot timestamp is the comparison timestamp.

---

28. Timestamped news

News is not simply a blob inside the evidence package.

Every news/event item must carry:

event_timestamp
retrieved_timestamp
source
event_type
affected_entities

This is required for later market-movement attribution.

---

29. Kickoff-relative research checkpoints

Do not define research checkpoints using weekdays.

Thursday, Sunday and Monday games occupy different information cycles.

Checkpoints should instead be relative to the individual game's kickoff.

Recommended initial windows:

OPENING

T-144h → T-96h

Use the earliest qualifying canonical snapshot in the window.

MID

T-60h → T-36h

Use the qualifying snapshot closest to:

T-48h

FINAL

T-6h → T-2h

Use the qualifying snapshot closest to:

T-3h

Exact windows should be validated in Week 0 and then frozen for the season.

---

30. Missing checkpoint markets

If a market does not exist within a checkpoint window, it does not receive that checkpoint.

Example:

A prop first appears at T-72h.

It does not qualify for OPENING.

Do not grab the T-72h line and relabel it as an Opening observation.

This means Opening research naturally represents markets posted sufficiently early.

That limitation should be stated in all season analysis.

---

31. Primary forecast scoring

The primary research forecast is the standardized:

OPENING

forecast.

This ensures all competitors are compared from the same stage of market development.

The headline research question is therefore:

«Do model forecasts add predictive information beyond the canonical and broader market during the relatively early, less mature phase of NFL player-prop price discovery?»

Do not overstate this as:

«Can AI beat the market?»

---

32. Secondary checkpoint scoring

MID and FINAL forecasts are scored separately.

These answer different questions.

OPENING

Can the model identify useful information before the market fully matures?

MID

Does any apparent advantage persist as more information and betting activity arrive?

FINAL

Does the model retain predictive information beyond a substantially more mature pregame market?

---

33. Market scoring

The market itself must be scored at every fixed checkpoint.

For every eligible observation calculate outcome scores for:

- GPT
- Claude
- Gemini
- canonical market
- same-line consensus when valid

Example season table:

Horizon| GPT| Claude| Gemini| Canonical| Consensus
Opening| Brier| Brier| Brier| Brier| Brier
Mid| Brier| Brier| Brier| Brier| Brier
Final| Brier| Brier| Brier| Brier| Brier

This prevents meaningless statements such as:

«GPT improved from Tuesday to Sunday.»

The relevant comparison is:

«GPT improved by X while the market improved by Y.»

---

34. Brier scoring

For a binary non-pushable Over event:

outcome = 1

if final statistic exceeds the line.

outcome = 0

if final statistic is below the line.

Brier score:

(model_probability_over - outcome)^2

Lower is better.

Use the exact same event definition for model and market forecasts.

---

35. Log loss

Log loss should also be retained as a secondary proper scoring rule.

This provides additional sensitivity to extreme overconfidence.

Do not rely on raw accuracy alone.

---

36. Calibration

Calibration should be analyzed across the standardized Forecast Lab sample.

Potential tools:

- reliability plots
- broad probability buckets
- Brier score
- log loss
- overconfidence analysis
- underconfidence analysis

Do not create tiny confidence buckets and make strong claims from three observations.

Even Forecast Lab remains a one-season exploratory dataset.

---

37. Model-vs-market disagreement

Every observation should calculate:

probability_disagreement =
model_probability_over
-
canonical_market_probability_over

Example:

Market: 53%
GPT: 61%

Disagreement: +8 percentage points

This is one of the central observations in the entire project.

It lets the system ask:

«When the model disagrees more strongly with the market, does that disagreement contain predictive signal?»

---

38. Forecast revisions

Never edit a forecast in place.

Every changed opinion creates a new "ForecastObservation".

Example:

forecast_v1
    ↓
forecast_v2
    ↓
forecast_v3

Each revision stores:

revision_parent_id
revision_reason
timestamp
new evidence snapshot
new market baseline

Possible reasons:

SCHEDULED_CHECKPOINT
NEWS_EVENT
INJURY_EVENT
LINE_MOVE
WEATHER_CHANGE
DEPTH_CHART_CHANGE
MODEL_REVIEW
OTHER

---

39. Contemporary market re-snapshotting

Every revision receives a new canonical market snapshot from that revision's timestamp.

Never compare:

Thursday model forecast

against:

Tuesday market baseline

Each observation contains:

model_probability_t
canonical_market_probability_t
evidence_snapshot_t

This preserves the meaning of model-market disagreement.

---

40. Updating quality

Updating skill is evaluated separately from Opening forecast skill.

The main standardized updating comparisons are:

OPENING → MID
MID → FINAL
OPENING → FINAL

Questions include:

- Did forecast accuracy improve?
- Did calibration improve?
- Did the model react in the correct direction?
- Did the magnitude of the update make sense?
- Did the model improve faster or slower than the market?
- Did model-market disagreement become more informative or disappear?

Event-driven revisions may also be studied descriptively.

Fixed checkpoints remain the primary cross-model comparison.

---

41. Event-driven revisions

Models may revise opinions between fixed checkpoints when meaningful events occur.

Examples:

major line movement
player ruled out
important teammate ruled out
starter announced
market reopening
significant weather change
meaningful beat-reporter information

These revisions are stored permanently.

They do not replace standardized checkpoint observations.

---

42. Meaningful-event detector

Create:

MarketEventDetector

Potential triggers:

line change >= configured threshold
price move >= configured threshold
injury designation change
player inactive
starter announced
major roster move
weather threshold crossed
market reopened
market withdrawn

Thresholds should be configurable.

---

43. Market-movement attribution

Line movement after a model forecast can occur for different reasons.

Do not simply label every favorable move as model skill.

After the relevant market closes, classify movement using:

NO_NEW_INFO
PUBLIC_INFO_ALREADY_AVAILABLE
NEW_INFO_AFTER_FORECAST
ATTRIBUTION_UNKNOWN

Interpretation:

NO_NEW_INFO

No identifiable major informational event explains the movement.

Potential evidence of market anticipation.

PUBLIC_INFO_ALREADY_AVAILABLE

Relevant public information existed when the model made its forecast and was included or available within its evidence environment.

This may represent information-processing or latency skill.

NEW_INFO_AFTER_FORECAST

Material new information emerged after the model forecast.

The model should not receive credit for anticipating movement caused by information that did not yet exist unless its forecast explicitly anticipated that scenario.

ATTRIBUTION_UNKNOWN

Insufficient confidence to classify.

This is expected to be common.

---

44. Attribution judge

GPT, Claude and Gemini must never classify attribution around their own forecasts.

Create an independent:

AttributionJudge

Possible implementation:

- deterministic rules first
- independent fourth model for ambiguous cases
- optional human review

Store:

judge_type
judge_model
judge_version
classification
classification_confidence
evidence_event_ids
attribution_note
review_status

Recommended automated confidence threshold:

0.75

Below threshold:

ATTRIBUTION_UNKNOWN

unless manually reviewed.

---

45. Attribution review queue

Do not manually inspect every observation.

Create a review queue for high-value cases such as:

- Pounce bets
- very large model-market disagreement
- major line movement
- season awards
- apparent extreme timing success
- attribution disagreement
- low classifier confidence

---

46. Common-coverage research cohort

Cross-model headline comparisons require identical observation sets.

A prop/checkpoint enters the research-grade common cohort only if all of the following exist:

- valid GPT forecast
- valid Claude forecast
- valid Gemini forecast
- valid canonical market baseline
- valid research outcome
- research-eligible line

If any component is absent, that row is stored but excluded from the headline common comparison.

Do not impute.

Do not carry old forecasts forward.

Do not substitute another sportsbook.

---

47. Research exclusion reasons

Store explicit reasons:

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

---

48. Coverage reporting

Every headline research metric should report coverage.

Example:

GPT Opening Brier: 0.218
Common eligible observations: 147 / 180
Coverage: 81.7%

Never present a score without making the denominator visible.

---

49. Correlated observations

Benchmark observations are not assumed to be statistically independent.

Multiple props may share:

- player
- quarterback
- offense
- defense
- game environment
- weather
- injuries
- pace
- coaching decisions

Therefore:

«147 observations does not mean 147 independent observations.»

Do not calculate naive confidence intervals assuming independence.

If uncertainty estimates are later produced, prefer cluster-aware approaches such as resampling at game level.

Week-level sensitivity analysis may also be useful.

Given a single NFL season, inferential claims should remain conservative.

---

50. Research-grade vs descriptive metrics

Every metric belongs to one of two classes.

Research-grade / standardized

Built from sufficiently repeated, standardized observations.

Examples:

- benchmark Brier score
- benchmark log loss
- model-vs-market disagreement
- opening forecast quality
- mid forecast quality
- final forecast quality
- standardized updating behavior
- broad calibration analysis

These may support cautious comparative findings.

---

Descriptive / narrative

Based on relatively few official wagers.

Examples:

- real-money ROI
- official W-L
- Pounce record
- stake-sizing record
- selection quality
- timing quality
- aggressive-wager performance
- Call Your Shot record
- execution quality

These describe what happened.

They should not be treated as proof of stable model skill from approximately 18 wagers.

---

51. Claim discipline

Acceptable:

«Gemini finished with the highest real-money ROI.»

Acceptable:

«Claude's six aggressive wagers lost money.»

Not justified from one season:

«Gemini is definitively the best bankroll manager.»

Not justified:

«Claude is systematically bad at aggressive staking.»

The season postmortem must distinguish observation from inference.

---

52. Batch vs isolated forecasting

A ten-prop structured forecast can be generated:

BATCHED

or:

ISOLATED

Batching is cheaper and operationally simpler, but it may introduce:

- cross-prop anchoring
- context dilution
- reduced reasoning depth
- ordering effects

Week 0 should evaluate both.

Use the same slate and evidence snapshots.

Compare:

- probability differences
- ranking consistency
- missingness
- schema failures
- confidence differences
- qualitative output quality

Week 0 is not expected to prove equivalence statistically.

Its purpose is to detect obvious distortion.

After Week 0, freeze the chosen methodology.

Possible compromise:

2–3 props per call

if full batching performs poorly.

---

53. Model abstraction

Use one common interface.

Example:

class CompetitorAgent:
    async def forecast_benchmark(...)
    async def create_watchlist(...)
    async def evaluate_open_market(...)
    async def review_event(...)
    async def make_final_decision(...)
    async def propose_stake(...)
    async def generate_public_comment(...)
    async def postmortem(...)

Implement:

OpenAIAgent
ClaudeAgent
GeminiAgent

Provider-specific behavior stays inside these adapters.

---

54. Structured model outputs

Consequential decisions must use validated structured output.

Example:

{
  "market_id": "abc123",
  "model_probability_over": 0.61,
  "recommended_side": "OVER",

  "confidence": 8.8,
  "uncertainty": "LOW",

  "canonical_market_probability": 0.535,
  "estimated_edge": 0.075,

  "urgency": "POUNCE",
  "risk_posture": "AGGRESSIVE",

  "public_reasoning": "...",
  "key_factors": [],
  "primary_concern": "...",
  "why_now": "..."
}

Malformed outputs:

1. request schema correction
2. retry
3. reject if still invalid
4. log failure

Do not silently repair consequential values.

---

55. Private reasoning

Do not attempt to extract or display private chain-of-thought.

Request explicit public summaries instead:

public_reasoning
key_factors
primary_concern
what_changed
why_now
stake_justification

These are displayable and auditable.

---

56. Market probability vs model probability

Every serious candidate should compare:

MODEL ESTIMATE
vs
DE-VIGGED MARKET ESTIMATE

Example:

Model: 61%
Canonical market: 53.5%
Estimated disagreement: +7.5 points

That difference is not automatically "true edge."

It is the model's estimated edge relative to the selected baseline.

Outcome and later market behavior determine whether that estimate appears informative.

---

57. Confidence

Confidence scale:

1.0–10.0

Suggested interpretation:

<5.0
No meaningful conviction

5.0–6.4
Low / probe conviction

6.5–7.4
Standard

7.5–8.4
Strong

8.5–10.0
Exceptional

Confidence should consider:

- strength of evidence
- model uncertainty
- estimated edge
- stability of assumptions
- data quality
- market quality

Confidence is not itself a probability.

---

58. Uncertainty

Use:

LOW
MEDIUM
HIGH

Two bets can have similar estimated edges but materially different uncertainty.

Higher uncertainty should generally encourage smaller stake requests.

---

59. Urgency

Urgency is separate from confidence.

States:

WATCH
LEAN
STRONG
POUNCE

WATCH

Interesting candidate.

No action.

LEAN

Positive view, insufficient conviction or edge.

STRONG

Ready for normal execution.

POUNCE

Model believes valuable pricing may disappear quickly and requests expedited execution.

High confidence does not automatically imply POUNCE.

POUNCE means urgency, not merely conviction.

---

60. Risk posture

Every official wager receives:

CONSERVATIVE
STANDARD
AGGRESSIVE

This describes how strongly the competitor intends to press relative to its normal sizing reference.

An aggressive stake requires explicit justification.

---

61. Stake sizing as a competitive skill

The model must answer two different questions:

«Is this bet attractive?»

and:

«Given my bankroll and uncertainty, how much should I risk?»

Stake sizing is intentionally part of the competition.

Models are allowed to develop different risk behavior.

The backend should not homogenize all three into identical stakes.

---

62. Kelly reference

Kelly Criterion is used as a mathematical reference point.

Because LLM probability estimates are uncertain, full Kelly should not be used.

Use configurable fractional Kelly.

Recommended Week 0 candidate:

1/5 Kelly

Other values may be tested:

1/4
1/8

Freeze the final fraction before Week 1.

---

63. Stake-sizing flow

Conceptually:

CURRENT AVAILABLE BANKROLL
        ↓
MODEL PROBABILITY
        ↓
MARKET PRICE
        ↓
ESTIMATED EDGE
        ↓
FULL KELLY REFERENCE
        ↓
FRACTIONAL KELLY REFERENCE
        ↓
MODEL CONFIDENCE + UNCERTAINTY
        ↓
MODEL REQUESTED STAKE
        ↓
RISK POSTURE
        ↓
HARD COMPETITION CAPS
        ↓
FINAL ALLOWED STAKE

Confidence and stake should be related.

Confidence must not replace the mathematics underneath.

---

64. Kelly reference vs requested stake

Always store three values:

kelly_reference_stake
model_requested_stake
final_allowed_stake

Example:

Kelly reference: $11.40
Model request: $14.00
Hard cap: $15.00
Final allowed: $14.00

Or:

Kelly reference: $11.40
Model request: $17.00
Hard cap: $15.00
Final allowed: $15.00

This allows bankroll-management behavior itself to be studied.

---

65. Confidence and stake

Confidence influences the model's requested sizing.

It should not be implemented as a simplistic backend formula such as:

confidence 9 = automatically bet 25%

Instead the model receives:

- bankroll
- edge
- Kelly reference
- uncertainty
- legal limits

and proposes a stake.

The system then validates it.

This preserves meaningful differences between competitors.

---

66. Standard maximum stake

Recommended starting rule:

20% of current available bankroll

for normal wagers.

Example:

Bankroll: $50
Standard cap: $10

---

67. Exceptional maximum stake

For genuinely exceptional / Pounce situations:

30% of current available bankroll

Example:

Bankroll: $50
Exceptional cap: $15

The model may bet less.

It may not exceed the hard cap.

---

68. Aggression is allowed

The competition should not artificially suppress aggressive bankroll decisions.

Example:

Bankroll: $50
Confidence: 9.3
Uncertainty: LOW
Estimated edge: large
Kelly reference: $10
Requested stake: $14
Risk posture: AGGRESSIVE

This is legal.

A large bet is not automatically bad.

The question is whether the size was justified.

---

69. No permanent dollar ceiling

Stake limits scale with bankroll.

Example:

Bankroll $15
20% = $3
30% = $4.50

Later:

Bankroll $50
20% = $10
30% = $15

Later:

Bankroll $100
20% = $20
30% = $30

Successful bankroll growth should create larger wagering capacity.

---

70. Stake contraction

Poor performance reduces wagering power naturally.

Example:

Bankroll $6

Standard cap:
$1.20

Exceptional cap:
$1.80

This is a core survival mechanic.

---

71. Available bankroll

Stake percentages are based on:

available settled bankroll

not:

- starting bankroll
- projected bankroll
- stale bankroll
- hypothetical winnings

Any unresolved exposure should be excluded from available cash.

---

72. Minimum stake

Use the actual sportsbook's practical minimum where possible.

Potential configured minimum:

$0.25

or:

$0.50

depending on sportsbook support.

---

73. Pounce ticket

A Pounce must be executable.

Required fields:

market
side
observed_line
acceptable_line_boundary
maximum_acceptable_price

estimated_probability
estimated_edge
confidence
uncertainty

kelly_reference_stake
requested_stake
bankroll_fraction
risk_posture

why_market
why_side
why_price
why_now
primary_risk

valid_until

Example:

GPT — POUNCE

Player X O52.5
Accept through 54.5
Max price -120

Bankroll: $50
Kelly reference: $10.80
Requested stake: $14
Risk: 28%

Confidence: 9.2
Uncertainty: LOW

Why now:
Relevant teammate ruled out and market has moved only two yards.

Expires: 6:30 PM

---

74. Pounce limits

Recommended:

Maximum 1 active Pounce ticket
per competitor per week

An expired, unexecuted Pounce may be replaced later unless season rules say otherwise.

Once a wager is executed, that is the competitor's official weekly wager.

---

75. Human execution

AI competitors never directly place bets.

The human user receives the ticket and records one outcome:

PLACED
MARKET_MOVED
UNAVAILABLE
MISSED_WINDOW
SKIPPED

If placed, record:

sportsbook
actual_line
actual_price
actual_stake
execution_timestamp

---

76. Requested vs executed wager

Never collapse these.

Store both.

Example:

Requested:
O52.5
Accept through 54.5
Max -120
Stake $4

Executed:
O53.5
-115
Stake $4

This difference is essential for evaluating execution quality.

---

77. Final decision deadline

Each competitor must eventually declare:

BET

or:

PASS

by the configured weekly deadline.

The final deadline should provide the human enough time to execute the ticket.

Exact operational timing should be finalized during Week 0.

---

78. Bankroll ledger

All bankroll movement occurs through:

BankrollTransaction

Types:

SEASON_START
STAKE
WIN_RETURN
PUSH_RETURN
VOID_RETURN
ADJUSTMENT

Never directly overwrite current bankroll without a ledger entry.

---

79. Bankruptcy

No real-money bailout.

When:

available bankroll < minimum possible wager

the competitor is financially eliminated.

Arena state:

BUSTED

Its real-money bankroll is frozen.

---

80. Post-bankruptcy continuation

A busted competitor remains part of the forecasting experiment and season story.

It should continue:

- benchmark forecasting
- open-market analysis
- weekly selection
- public commentary

For entertainment continuity, a separate post-bust paper account may be initialized under clearly labeled rules.

That account must never be merged into real-money standings.

Real-money bankruptcy remains permanent for that season.

---

81. Real-money settlement

The actual sportsbook's grading determines:

real bankroll

If the sportsbook grades a wager:

WIN
LOSS
PUSH
VOID

the bankroll follows that settlement.

Store:

sportsbook_result
sportsbook_payout
sportsbook_settled_at

---

82. Research settlement

Forecast Lab uses one designated research outcome source.

Recommended:

«Official NFL statistical record, or a data provider explicitly mirroring official NFL statistics.»

Research results are frozen at:

T+72 hours after game completion

or another season-configured delay finalized before Week 1.

Store:

research_stat_value_at_lock
research_outcome_at_lock
research_locked_at

Once locked, season analysis does not silently change.

---

83. Later stat corrections

If official statistics change after the research settlement lock, preserve the correction separately:

later_corrected_stat
later_corrected_outcome
correction_timestamp

Do not rewrite the original research result invisibly.

---

84. Settlement divergence

It is possible for:

SPORTSBOOK RESULT

and:

RESEARCH RESULT

to differ.

That is acceptable.

Store both.

Example:

Research result: LOSS
Sportsbook settlement: WIN
Reason: later stat/provider difference

This becomes an auditable edge case instead of contamination.

---

85. Official performance dimensions

Do not reduce the experiment to bankroll alone.

Track distinct dimensions.

Forecast quality

Were probabilities informative relative to outcomes and the market?

Updating quality

Did probabilities change appropriately as information arrived?

Discovery quality

Did the model find useful opportunities outside the benchmark slate?

Selection quality

Did it choose intelligently among its own forecasts?

Sizing quality

Did it risk an appropriate amount?

Timing quality

Did it commit before value disappeared?

Execution quality

What line and price were actually obtained?

Outcome

Did the wager win?

These concepts should remain distinct.

---

86. Closing-line value

For official wagers track:

line_first_identified
line_at_pounce
line_at_lock
line_at_execution
closing_line

price_first_identified
price_at_pounce
price_at_execution
closing_price

Derived metrics may include:

CLV
percentage beating close
average line improvement
average price improvement

Because only approximately 18 official bets exist per model, these remain descriptive over one season.

---

87. Decision quality vs result

Every official wager should retain separate fields for:

RESULT

and:

PROCESS / DECISION CONTEXT

Example:

Result: LOSS
Beat closing line by 3.5 yards

This can be described as:

«Good market capture, bad outcome.»

Similarly:

Result: WIN
Took a materially worse number than close

should not automatically become:

«Great decision.»

---

88. Stake-sizing analytics

Track:

average stake
median stake
average bankroll percentage risked
largest dollar wager
largest bankroll percentage wager
requested stake vs Kelly reference
final stake vs Kelly reference
ROI by risk posture
performance by confidence level
maximum drawdown

Important descriptive question:

«Did larger stated conviction correspond to better outcomes?»

Do not claim stable sizing skill from a handful of wagers.

---

89. Pounce analytics

Track:

Pounce count
Pounce win rate
Pounce ROI
Pounce CLV
Pounce expiration rate
average stake percentage
average subsequent market movement
false urgency rate

Example false urgency:

«Model requested immediate execution, but the same or better market remained available for 36 hours.»

---

90. Pass analytics

Store all PASS decisions.

Potential descriptive metrics:

pass rate
best avoided loss
worst missed opportunity
best candidate at time of pass
subsequent closing movement
hypothetical outcome

A pass is not automatically good or bad.

---

91. Call Your Shot

After an opponent's official wager becomes public, another competitor may issue:

CALL YOUR SHOT

This is a public fade/challenge.

Example:

«Claude publicly fades GPT's Jefferson Over.»

It does not affect bankroll.

Store:

challenger
target_competitor
target_wager
prediction
confidence
result

---

92. Same-market wagers

Multiple competitors may independently choose the same wager.

Allow it.

This is meaningful convergence.

---

93. Opposing wagers

Competitors may wager opposite sides of the same market.

Allow it.

These moments are especially valuable for the Show layer.

---

94. Unanimous Pick Alert

If all three independently choose the same side of the same market:

UNANIMOUS PICK

Trigger an arena-wide event.

No scoring or bankroll rule changes.

Track unanimous performance separately.

---

95. Emergent reputations

Do not assign personalities in advance.

Potential labels should emerge from behavior.

Examples:

EARLY MOVER
PATIENT
LINE CHASER
AGGRESSIVE
CONSERVATIVE
HIGH-CONVICTION
VOLATILE
CLV LEADER
POUNCE HAPPY
PASS MASTER
BIG SWINGER

Only assign labels after sufficient descriptive history exists.

These are entertainment descriptors, not scientific findings.

---

96. Trash talk

Trash talk is generated separately from betting analysis.

It receives only public competition state.

It may use:

- standings
- executed wagers
- results
- public challenges
- rival comments
- streaks

It must not receive private analytical state.

It should never affect later handicapping prompts.

---

97. Weekly receipt

Every week generates a permanent recap object.

Possible fields:

wagers
passes
results
bankroll changes
largest wager
highest bankroll percentage risked
best CLV
worst CLV
best pass
biggest regret
best Pounce
worst Pounce
best market capture
biggest bad beat
Call Your Shot results
unanimous event
weekly awards
updated standings

---

98. Season Ledger

Every meaningful event is permanently reconstructable.

Example:

Week 8
Thursday 2:17 PM

Gemini issued POUNCE.

Bankroll: $48.70
Requested stake: $12
Risk: 24.6%
Confidence: 9.2

Canonical line: 58.5
Execution line: 59.5
Closing line: 63.5

Result: WIN

The season should be replayable later.

---

99. Competition events

Create:

CompetitionEvent
- id
- timestamp
- season_id
- week_id
- competitor_id
- event_type
- payload

Potential events:

WEEK_OPENED
CHECKPOINT_CAPTURED
FORECAST_CREATED
FORECAST_REVISED
WATCHLIST_CREATED
PROP_WATCHED
PROP_DROPPED
NEWS_EVENT
MARKET_MOVED
CONFIDENCE_CHANGED
STAKE_CHANGED
POUNCE_ISSUED
TICKET_LOCKED
TICKET_EXPIRED
BET_EXECUTED
PASS_DECLARED
CALL_YOUR_SHOT
UNANIMOUS_PICK
GAME_STARTED
PROP_WON
PROP_LOST
BANKROLL_CHANGED
BANKRUPTCY
RESEARCH_SETTLED
SPORTSBOOK_SETTLED
WEEK_SETTLED
TRASH_TALK_POSTED
AWARD_GRANTED

This event stream will eventually drive much of the arena.

---

100. Arena-facing API

Create presentation-specific read models.

Example:

GET /arena/state
GET /arena/events
GET /leaderboard
GET /competitors/{id}
GET /weeks/{id}

The arena should never reconstruct competition logic itself.

---

101. Future avatar state

Research phase:

GPT
WATCHING 4

Strong lean:

GPT
TOP LEAN
PLAYER X O52.5
CONF 7.6

Pounce:

GPT
POUNCE
PLAYER X O52.5
CONF 9.1

Executed:

GPT

O53.5 @ -115

$14 / $50
28% BANKROLL

AGGRESSIVE

Live:

GPT
48 / 54 YDS

Settled:

WIN
+$12.17

---

102. Confidence and risk visualization

Confidence and wager size should be visually associated without implying they are identical.

Example:

CONFIDENCE
█████████░
9.1

BANKROLL RISK
██████░░░░
28%

This immediately communicates whether a competitor is pressing.

---

103. Arena reactions

Potential events:

POUNCE
→ station alarm

SEASON-HIGH WAGER
→ large wager animation

UNANIMOUS PICK
→ arena-wide alert

OPPOSING PICKS
→ head-to-head presentation

BANKROLL LEAD CHANGE
→ leaderboard event

WIN
→ celebration

LOSS
→ reaction

BANKRUPTCY
→ station shutdown

These are visualization only.

---

104. Operational control panel

Before building the 3D arena, build an ugly but effective Commissioner dashboard.

Display:

current week
system health
data freshness
canonical-market status
benchmark slate
checkpoint coverage
model status
private watchlists
forecast observations
market movement
confidence
stake recommendations
Pounce requests
pending execution tickets
executed wagers
passes
bankrolls
settlement status
event log

Controls:

open week
capture checkpoint
refresh data
run benchmark forecast
run model review
trigger event review
record execution
mark ticket unavailable
settle wager
override settlement
lock research result
close week

---

105. Notifications

Create:

NotificationService

Possible channels:

browser push
mobile push
Discord
email
SMS

The betting engine emits notification events.

It does not depend directly on a specific channel.

---

106. Reproducibility

Every consequential AI action should record:

competitor
provider
exact model
timestamp
prompt_version
schema_version
evidence_snapshot_id
market_snapshot_id
raw structured response
validated response
bankroll_at_decision

For executed bets additionally store:

bankroll_at_execution

The goal is complete reconstruction.

---

107. Prompt separation

Do not use one giant agent prompt for everything.

Maintain separate prompt families:

BENCHMARK_FORECASTING
OPEN_MARKET_RESEARCH
MARKET_EVENT_REVIEW
STAKE_SIZING
FINAL_DECISION
POSTMORTEM
PUBLIC_COMMENTARY
TRASH_TALK

Competition prompts remain analytical.

Entertainment prompts may be creative.

---

108. Historical context shown to models

Competitors may receive strategically relevant personal history:

current bankroll
recent wagers
recent stake sizes
drawdown
historical forecast performance
historical calibration
historical performance by market type

Do not feed narrative pressure into analytical prompts.

Bad:

«You've lost three straight and everyone is laughing at you. Make a comeback pick.»

Good:

«Current bankroll: $8.20. Recent forecasts and wagers are attached.»

---

109. Sports-data provider abstraction

Create interfaces such as:

OddsProvider
StatsProvider
NewsProvider
InjuryProvider
WeatherProvider

Provider-specific formats must be normalized immediately.

The rest of the application should operate only on internal models.

---

110. Research outcome provider

Create a dedicated interface:

ResearchSettlementProvider

This source is distinct from the actual sportsbook.

Its identity and rules are frozen in "SeasonRules".

---

111. Automated sportsbook interaction

Do not automate sportsbook account access.

No:

- sportsbook credential storage
- automated login
- automated wager placement
- authenticated sportsbook scraping

The system produces an executable ticket.

The human user remains the execution layer.

---

112. Initial database entities

Recommended starting entities:

seasons
season_rules
weeks
competitors

games
players
prop_markets
prop_quotes
market_snapshots

news_items
injury_events
weather_snapshots
market_events

evidence_snapshots
forecast_observations

agent_sessions
watchlists
watchlist_entries

stake_recommendations
tickets
wagers
settlements

bankroll_transactions

competition_events
attribution_reviews

call_your_shots
weekly_receipts
awards
trash_talk_messages

Do not over-normalize until one full mocked week works.

---

113. Primary analytics hierarchy

The project should report results in roughly this order.

Forecast Lab

1. Opening Brier vs market
2. Opening log loss vs market
3. Mid Brier vs market
4. Final Brier vs market
5. model-market disagreement
6. calibration
7. updating quality
8. attribution categories

Competition

1. real bankroll
2. profit/loss
3. ROI
4. W-L-P
5. CLV
6. stake behavior
7. timing
8. Pounce behavior
9. PASS behavior
10. opportunity capture

Show

1. standings
2. streaks
3. awards
4. rivalries
5. records
6. memorable events

---

114. Methodological honesty

The project should not begin with the assumption:

«LLMs can beat player-prop markets.»

The experiment asks whether they can demonstrate useful signal.

Possible valid outcome:

«None of the models added measurable forecasting information beyond the market.»

That is still a successful experiment.

Possible outcome:

«One model showed useful early-market disagreement that disappeared near kickoff.»

Also interesting.

Possible outcome:

«Models forecast reasonably but consistently selected the wrong opportunities for their official bets.»

Also interesting.

The system should be designed so failure is informative.

---

115. Primary research claim boundary

The strongest intended initial question is:

«Do frontier LLM forecasts add useful predictive information beyond de-vigged market probabilities during early NFL player-prop price discovery?»

It is not:

«Can AI beat sportsbooks?»

And certainly not:

«We proved Model X can make money betting football.»

One season cannot responsibly establish that.

---

116. MVP success definition

The backend MVP succeeds if this complete flow works:

1. Week opens.
2. Sports markets load.
3. Eligible benchmark slate is generated.
4. Immutable Opening snapshot is captured.
5. GPT, Claude and Gemini forecast the same slate.
6. Market baseline is stored for every observation.
7. Models independently build private watchlists.
8. Open-market candidates create ForecastObservations too.
9. New information arrives.
10. Relevant opinions are revised without overwriting history.
11. Mid checkpoint occurs.
12. Final checkpoint occurs.
13. A model may issue a Pounce.
14. Stake request is compared to Kelly reference and competition caps.
15. User receives an executable ticket.
16. User records actual execution.
17. Each competitor eventually records BET or PASS.
18. Games occur.
19. Sportsbook wagers settle.
20. Research outcomes lock from the designated stats source.
21. Bankrolls update.
22. Forecast Lab scores models and markets on identical cohorts.
23. Attribution is classified.
24. Weekly receipt is generated.
25. Entire week can be reconstructed from stored data.

If this is compelling with a plain admin interface, the 3D arena is worth building.

---

117. Build sequence

Phase 0 — Finalize rules

Create:

RULES.md

Freeze all season-level decisions.

---

Phase 1 — Domain skeleton

Build:

Season
SeasonRules
Week
Competitor
Bankroll ledger
Ticket
Wager
Settlement
CompetitionEvent

Use fake markets.

---

Phase 2 — Forecast Lab foundation

Build:

PropMarket
PropQuote
EvidenceSnapshot
ForecastObservation
Research eligibility
Brier scoring
Market scoring
Common cohorts

---

Phase 3 — AI adapters

Implement:

OpenAIAgent
ClaudeAgent
GeminiAgent

Use mocked evidence first.

---

Phase 4 — Week 0 methodology harness

Implement:

batch vs isolated tests
checkpoint timing
coverage reports
de-vig verification
revision chains
attribution workflow
fake settlement

---

Phase 5 — Risk engine

Build:

probability conversion
edge calculation
Kelly reference
fractional Kelly
confidence
uncertainty
risk posture
stake validation
20% / 30% caps

---

Phase 6 — Real sports-data adapters

Add:

odds
stats
news
injuries
weather
research settlement source

---

Phase 7 — Weekly orchestration

Implement:

Opening
Mid
Final
watchlists
event-triggered reviews
Pounce
BET/PASS

---

Phase 8 — Human execution

Build:

notifications
pending tickets
execution confirmation
line validation
price validation
stake recording
expiration

---

Phase 9 — Settlement

Implement separate:

sportsbook settlement
research settlement
late stat corrections

---

Phase 10 — Analytics

Build:

Brier
log loss
market comparison
model disagreement
coverage
calibration
updating analysis
CLV
bankroll
stake analytics
opportunity capture

---

Phase 11 — Season-story system

Build:

weekly receipts
Call Your Shot
Unanimous Pick
awards
emergent reputations
trash talk
season ledger

---

Phase 12 — Arena API

Expose presentation state and event streams.

---

Phase 13 — 3D arena

Only after the underlying competition is working:

Astra assets
avatars
stations
central arena
floating pick displays
confidence meters
bankroll-risk meters
animations
live prop progress

---

118. Items to finalize during Week 0

These remain deliberate configuration decisions rather than unresolved architecture.

Canonical sportsbook

Select one and freeze it.

Research settlement source

Select one and freeze it.

Exact checkpoint windows

Current proposal:

Opening:
T-144h → T-96h

Mid:
T-60h → T-36h

Final:
T-6h → T-2h

Validate against real prop availability.

Benchmark slate size

Current recommendation:

10

Batch methodology

Choose:

10 in one call
2–3 per call
isolated calls

after Week 0 testing.

Fractional Kelly

Current recommended test:

1/5 Kelly

Freeze before Week 1.

Minimum stake

Set according to sportsbook capability.

Normal stake cap

Current recommendation:

20% bankroll

Exceptional/Pounce stake cap

Current recommendation:

30% bankroll

Pounce policy

Current recommendation:

1 active Pounce per competitor per week

Final weekly execution deadline

Set after testing actual workflow.

Notification channel

Choose the fastest practical method for the human user.

---

119. Core philosophy

The models are not characters pretending to gamble.

They are competitors making real analytical decisions under a small but meaningful bankroll constraint.

The system should reward:

forecast quality
information processing
market awareness
price sensitivity
good updating
disciplined selection
appropriate confidence
appropriate aggression
stake discipline
good timing
survival

Aggression is not inherently bad.

Conservatism is not inherently good.

Winning is not automatically evidence of a good decision.

Losing is not automatically evidence of a bad one.

The project should ultimately be capable of revealing statements like:

«GPT was the best early forecaster but poor at selecting among its own edges.»

«Claude rarely showed large model-market disagreement but updated extremely well when new information arrived.»

«Gemini's forecasts were mediocre overall, but its Pounce timing repeatedly captured strong prices.»

Those are far more interesting than simply:

«GPT finished with the most money.»

And then the Show layer gets to turn all of that into three avatars talking shit in a 3D arena.

---

120. First engineering deliverables

Before implementing the full application, produce three documents from this constitution.

"RULES.md"

Convert all competition and methodology rules into explicit machine-relevant rules.

Resolve every Week 0 configuration before Week 1.

---

"ARCHITECTURE.md"

Define:

module boundaries
service responsibilities
API boundaries
background jobs
checkpoint scheduling
AI orchestration
risk engine
event system
notification flow
settlement flow
failure handling
audit strategy

---

"DATABASE.md"

Define the PostgreSQL schema and relationships for:

markets
snapshots
forecast observations
revision chains
competitor state
stake recommendations
tickets
wagers
settlements
bankroll transactions
research cohorts
event attribution
competition events
weekly receipts

Then implement a complete mocked NFL week.

Only after that mocked week works end-to-end should real external data providers be connected.

The first real milestone is not:

«Get Claude to pick a football prop.»

It is:

«Build a system that can reconstruct exactly what every competitor knew, believed, changed, requested, risked, executed and achieved at every meaningful point in the week — while comparing those forecasts fairly against what the market knew at the same time.»

Everything else sits on top of that.
