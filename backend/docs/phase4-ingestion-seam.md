# Phase 4 — Real Market Data Ingestion Seam

**Status:** ACCEPTED. Market-side questions closed by the 2026-09-17 validation
probe. Implemented in Phase 4A.1.
**Scope:** the market-data provider boundary.

Player identity, team and position are **out of scope here** and are governed by
the companion document
[`phase4-roster-identity-seam.md`](phase4-roster-identity-seam.md) — the probe
confirmed this vendor supplies a display name only.

This document is an engineering contract, not a proposal. It is the product of a
design-only review round conducted before any Phase 4 code was written,
specifically to prevent a vendor's response shape from leaking into Forecast Lab,
`MarketSnapshotService`, or the domain layer. The Odds API is the *first
implementation* of this seam, never its definition.

Where this document says MUST or NEVER, that is a reviewed decision, not a
preference. Where it says OPEN, the answer is blocked on the provider-validation
probe (§14) and MUST NOT be guessed at in code.

---

## 1. Fixed assumptions

Decided before this seam was designed; not re-litigated here.

| Item | Value |
| --- | --- |
| Initial provider | The Odds API v4 |
| Initial sport | NFL |
| Canonical sportsbook | `DRAFTKINGS` (already frozen in `SeasonRules`) |
| Internal stat families | `passing_yards`, `passing_touchdowns`, `rushing_yards`, `receptions`, `receiving_yards` |
| Checkpoint windows | unchanged (`OPENING` 144–96h, `MID` 60–36h, `FINAL` 6–2h pre-kickoff) |
| Credential | `THE_ODDS_API_KEY`, Railway backend Variables only |
| Validation probe | free tier, before any paid work |
| Backfill tier | upgrade to 100K before paid historical work |
| Live acceptance | runs from inside Railway, never from a dev sandbox |

The internal stat families above match `SeasonRules.supported_prop_types` exactly
as seeded in `app/db/repositories/season_repository.py`. They are our vocabulary.
Vendor market keys are translated into them at the adapter and never travel
further.

---

## 2. Two findings that motivated this round

Both were verified against the code on branch
`claude/prop-betting-league-constitution-i9vemh`, not inferred.

### 2.1 `quotes_as_of` selects on the wrong clock

`app/db/models/markets.py` gives `PropQuote` exactly one timestamp,
`retrieved_at`. And `app/db/repositories/market_repository.py` filters on it:

```python
def quotes_as_of(self, market_id, as_of) -> list[PropQuote]:
    .where(PropQuote.market_id == market_id, PropQuote.retrieved_at <= as_of)
    .order_by(PropQuote.retrieved_at.desc())
```

That is the sole input to every canonical and de-vigged `MarketSnapshot`. A
backfill retrieved on 20 September for a 5 September snapshot would be *excluded*
from the 5 September checkpoint (`retrieved_at` 09-20 > `as_of` 09-05) and
silently *included* in every checkpoint after 20 September, presenting two-week-old
prices as current market state.

This is not a future risk. It is the behaviour of the only quote-selection path
that exists, and historical ingestion would trip it immediately.

The ordering is load-bearing in a second place, `app/forecast_lab/market_snapshot_service.py`:

```python
# One quote per book: the most recent as-of `taken_at` (quote_rows
# is already ordered retrieved_at desc).
```

Both sites, and their tests, MUST be updated together. Leaving that comment
stale invites a future reader to "fix" the filter back.

### 2.2 `capture_checkpoint` is a live-window operation

`app/forecast_lab/checkpoint_service.py`:

```python
if now > run.window_end:
    run.status = "MISSED"
    return run
...
snapshot = snapshot_service.build_snapshot(..., taken_at=now)
```

A 20 September backfill of a 5 September checkpoint hits the `MISSED` branch and
returns before building anything. Fixing the time model in §3 does not reach
this. Historical reconstruction therefore needs its own path (§12), and the live
checkpoint rules MUST NOT be weakened, relaxed, or made conditional to
accommodate it.

---

## 3. Time model

Four distinct times. They are never conflated, and no code may substitute one for
another.

| Field | Meaning |
| --- | --- |
| `as_of_at` | The time this observation represents. |
| `retrieved_at` | When our process received and persisted the response. |
| `provider_market_updated_at` | Vendor market-level `last_update`. Nullable. Diagnostic only. |
| `CheckpointRun.target_time` | Unchanged. What a checkpoint asks for. |

`as_of_at` is derived as follows:

- **Current pull:** one stable capture timestamp for the accepted quote-fetch
  observation. Stamped once per observation, not per HTTP response.
- **Historical pull:** the provider snapshot timestamp returned for the requested
  as-of query. The historical API returns the closest snapshot at or earlier than
  the requested date; that returned timestamp is what establishes the quote was
  observable at that historical point. NEVER our clock.

A `PropQuote` row therefore means:

> At this observation time, this provider showed us this book / player / stat /
> line / price state.

It explicitly does **not** mean "this is when the provider believes the
underlying market last changed." That is a different fact, it lives in
`provider_market_updated_at`, and it MUST NOT drive checkpoint selection.

The rule is: **bind the market-level value when present.** That is the
granularity at which a specific prop market moves; a bookmaker-level stamp says
only that something in that book changed.

Evidence, kept deliberately at the level of certainty it actually has:

- The vendor's documented NFL example shows `last_update` at both the bookmaker
  level and the market level.
- The 2026-09-17 validation probe observed **market-level only** in that
  particular event-odds payload — no bookmaker-level field was present.

Neither observation defines the endpoint's contract. Do not infer from one
payload that bookmaker-level timestamps cannot appear here, and do not infer
from one doc example that they always will. Different endpoint shapes and future
vendor changes can differ; the probe reports which levels are present so the
binding is confirmed rather than assumed.

### 3.1 Two different ages — never conflate them

This distinction was got wrong once, in the analysis of the first real
ingestion run, and the error is easy to repeat because both numbers are
measured in seconds and both feel like "freshness".

| metric | formula | role |
| --- | --- | --- |
| **observation age** | `checkpoint/snapshot taken_at − PropQuote.as_of_at` | how old OUR OBSERVATION is when a checkpoint consumes it. **This** is what a live freshness rule is built on. |
| **provider market-change age** | `PropQuote.as_of_at − provider_market_updated_at` | how long since the BOOK last moved this market. **Diagnostic only.** |

The second MUST NOT drive checkpoint eligibility, and a threshold MUST NOT
be derived from it. A book can leave a line untouched for forty-five
minutes; if we successfully re-fetch it one second before `FINAL`, that is
a **fresh observation of a quiet market**, not a stale quote. Treating
market-change age as staleness would reject exactly the markets that are
most settled.

This is the same fact §4 relies on when it retains repeated unchanged
observations: an unchanged market observed again is new information about
our feed, not a duplicate.

Where the live design fetches immediately before a snapshot or checkpoint
capture, the observation age is near zero by construction. The tolerance
is therefore an **operational stale-data guard** for a failed or missed
refresh — not a measure of how recently a sportsbook changed its odds.

**Invariants.** `as_of_at <= retrieved_at`, always — a provider claiming a
snapshot from the future is a malformed response, not a quote. All timestamps are
timezone-aware UTC at the boundary; the adapter converts, and nothing downstream
sees a naive datetime or a vendor date string.

### 3.2 The freshness rule — Phase 4A.3

Freshness is decided **per selected sportsbook quote**, on observation age
and on nothing else.

`MarketSnapshotService` already selects the newest quote per book as of
`taken_at`. The gate is applied to that selection:

```
age = taken_at − quote.as_of_at
stale if age > max_observation_age_seconds
```

Three consequences, all recorded on the snapshot row rather than left to
be re-derived:

1. **A stale non-canonical book is excluded from the whole snapshot** —
   consensus probability *and* `market_median/min/max_line` *and*
   `number_of_books`. Half-excluding it would leave a sixteen-hour-old
   price widening a line context that reads as "what the market looks like
   right now." Its `PropQuote` row is untouched: exclusion is a read-side
   decision, never a retraction of an observation we genuinely made.

2. **A stale canonical book invalidates the baseline and promotes
   nobody.** RULES.md §16 forbids silently substituting another
   sportsbook, and "too old to trust" is a form of unavailable. The fresh
   books still supply market context — that is diagnostic, not a baseline,
   so losing canonical does not have to blind the snapshot.

3. **`canonical_quote_stale` is separate from
   `is_valid_canonical_baseline == false`.** "The book was quoting, we
   just had nothing recent enough" and "the book was not in the feed at
   all" are different failures, and only one of them is an ingestion
   problem. A single validity flag cannot tell them apart.

**Measured against `captured_at`, never `target_time`.** `target_time` is
scheduling *intent*. A capture that fires an hour late is still a real
capture, and its observations are only stale relative to when it actually
ran. Measuring against `target_time` would report the scheduler's lateness
as market staleness and throw out quotes that were fresh at capture. The
gap between the two is a separate, separately-named quantity:

| metric | formula |
| --- | --- |
| quote observation age | `MarketSnapshot.taken_at − selected PropQuote.as_of_at` |
| scheduler offset | `CheckpointRun.captured_at − CheckpointRun.target_time` |

`capture_checkpoint` passes one `now` to both `build_snapshot(taken_at=)`
and `run.captured_at`, so these are the same clock by construction.

**The threshold is not frozen yet, and there is deliberately no default.**
`max_observation_age_seconds` is a caller-supplied parameter on
`MarketSnapshotService` and `capture_checkpoint`; `None` disables the gate
and is the default, which reproduces the Phase-2 behaviour exactly. It is
*not* a `SeasonRules` column yet. No defensible number exists — the two
real DET @ BUF runs are 15h51m apart, which says what a missed refresh
looks like but nothing about what a normal one does. A plausible-looking
default chosen now would be a guess frozen into the research record, and
the column that records which threshold was in force exists precisely so
that guess could never be silently rewritten later.

#### Selection provenance

`MarketSnapshot.selected_quotes` (JSONB, schema
`market_snapshot_selection_v1`) freezes the exact `PropQuote` ids the
snapshot consumed and the ones it refused, each with its `as_of_at` and
computed age.

Re-deriving the selection later is **not** a safe substitute. The query
filters `as_of_at <= taken_at`, which looks reproducible — but a
historical backfill (Phase 4B) can legitimately insert rows with an
`as_of_at` *earlier* than a snapshot already taken, and the same query
then returns a different answer for the same snapshot. A record that
happens to be right today would be indistinguishable from one that has
since drifted.

It is written whether or not a gate is in force: provenance is not
conditional on a rule being active.

**Invariant:** `number_of_books + stale_books_excluded` equals the number
of books that had any quote at `taken_at`. A book must never fall out of
both counts — that is how "the feed dropped a book" turns into a snapshot
that merely looks thin.

### 3.3 The live choreography — Phase 4A.4

The production contract for a checkpoint:

```
preflight                    DB reads only. May this checkpoint work?
    |  ELIGIBLE only
    v
refresh market data          network, OUTSIDE any DB transaction,
    |                        bounded explicit retries
    v
persist immutable PropQuotes, COMMIT
    |
    v
capture checkpoint           DB reads only, at the real captured_at
```

**A paid refresh happens only when the checkpoint could use it
(Phase 4A.5).** The preflight is read-only and runs first. An
already-CAPTURED checkpoint is immutable, so new quotes could never reach
it; a too-early or expired one will not capture at all. Refreshing in any
of those cases buys nothing and costs credits — and a scheduler that
re-ticks pays again on every tick. This is a cost-integrity invariant.

4A.4 shipped this the wrong way round: it refreshed first and discovered
the disposition afterwards. The eligibility rule now lives in ONE pure
module (`checkpoint_window.py`) used by both the preflight and
`capture_checkpoint`, because two copies would let them disagree — and the
disagreement would be paid for in Odds API credits.

The preflight deliberately writes nothing, not even the PENDING
`CheckpointRun` row. Creating state as a side effect of asking a question
would mean a too-early poll silently changed the record it was only meant
to read; `capture_checkpoint` still does that bookkeeping afterwards,
unchanged.

`app/marketdata/checkpoint_cycle.py::run_checkpoint_cycle` is the one
scheduler-ready entry point. It is a callable job, not a scheduler:
something else decides when to call it.

**Provider HTTP never enters `capture_checkpoint`.** A network call inside
a capture holds a transaction open across an unbounded wait, and a
provider timeout aborts a capture that has already written half a slate's
snapshots — a failure that presents as a database problem and gets
diagnosed as one. There is an import-graph guard
(`test_capture_has_no_network.py`) walking every module transitively
reachable from the capture path and asserting none of them can reach an
adapter package or an HTTP client. It walks the graph rather than one
module's import list because the dangerous version of this regression is a
helper three modules down growing an adapter import.

**The capture clock is read AFTER the refresh settles.** Reading it first
reproduces the 4A.2 bug exactly: every snapshot claims a `taken_at`
earlier than the quotes the run just wrote, so it cannot see them.

**A failed refresh does not abort the capture.** This is the point of
having a tolerance at all. A capture that runs on last-known observations
and labels them stale is information; a capture that does not happen is a
hole in the research record. The gate decides whether those observations
are usable and the report says the refresh failed.

**Retries happen BEFORE the capture, never after it (MODEL B).** Because
`capture_checkpoint` is idempotent once CAPTURED, "retry by calling the
cycle again later" cannot work: the first call has already consumed the
checkpoint. A single transient timeout would otherwise permanently cost
FINAL, the highest-value checkpoint. Attempts are therefore bounded,
explicit and individually audited inside one call — each is its own
provider call with its own `ProviderCall` row, because "how many times did
we ask, and what did each attempt say" is research provenance rather than
an implementation detail. An adapter-level retry would make three billed
calls look like one.

Two constraints on the loop:

- **Not every failure is retryable.** `AUTHENTICATION_ERROR`,
  `QUOTA_EXHAUSTED` and `QUOTA_RESERVE_EXHAUSTED` cannot succeed on a
  retry. `MALFORMED_RESPONSE` is the sharpest case: the provider
  *answered* and we were **billed**, so the same request yields the same
  unusable payload and a retry pays again for it.
- **Retries must never consume the window they protect.** A guard stops
  the loop when the next backoff would leave too little of the window to
  capture in; turning a recoverable failure into a MISSED checkpoint is
  worse than the failure.

Both the tolerance and the retry budget are frozen in `SeasonRules`
(§3.7), because both materially change which market state may reach an
irreversible capture.

The cycle reports, separately and never merged:

| field | meaning |
| --- | --- |
| `refresh_started_at` / `refresh_completed_at` | the network window |
| `refresh_to_capture_seconds` | `captured_at − refresh_completed_at` |
| `captured_at` | what the gate measures against |
| `target_time` | scheduling intent |
| `scheduler_offset_seconds` | `captured_at − target_time` |
| `latest_by_book` | the freshest observation per book, and whether it was kept |
| `observation_ages_seconds` | per selected quote |

`refresh` is injected rather than constructed inside the cycle, so a
FAILING refresh in a test is a callable that returns `ok=False` — no
network, no credits, and the failure paths are exercisable at all.

---

### 3.4 Calibration is read-only, and never captures

`capture_checkpoint` is deliberately idempotent once a run reaches
CAPTURED. Capturing a durable game's FINAL to try a candidate threshold
would permanently freeze experimental `MarketSnapshot` and
`EvidenceSnapshot` artifacts onto a real game, and there is no second
attempt. **Calibration therefore never captures.**

`app/forecast_lab/calibration.py::preview_thresholds` scores candidate
tolerances against already-persisted quotes and writes nothing — no
`MarketSnapshot`, no `CheckpointRun`, no `EvidenceSnapshot`. A structural
test asserts it contains no session writes and does not name
`build_snapshot`, `capture_checkpoint` or `CheckpointRun` in its code.

It does **not** re-implement the rule. Every candidate is scored through
`MarketSnapshotService.selection_plan`, which is the same call
`build_snapshot` makes. A preview built on its own copy of the rule is
evidence about the copy, not about the rule; there is a test asserting the
preview and a real capture reach identical verdicts on identical data, and
an AST guard asserting `quote_selection.py` is the only module in the
codebase that ordering-compares an observation age against a tolerance.

The rule itself lives in `app/forecast_lab/quote_selection.py` as a pure
function over `QuoteObservation` values. `provider_market_updated_at` is
not a field on that type, so the rule cannot consult it even by accident —
the forbidden field is simply not reachable from the decision.

#### What the tolerance actually is

Not something to be discovered by observing arbitrary quote ages. In the
intended workflow the refresh immediately precedes the capture, so normal
observation age is ~0 and every candidate threshold scores identically.

The threshold is an **operational fallback grace period**: how old we are
willing to let the last successful observation be when the refresh that
should have preceded this capture did not produce one. Only the failure
cases speak to it.

It is specifically NOT a network-timeout allowance and NOT a retry
allowance. A current quote stamps `as_of_at` at **response receipt**, so a
slow-but-successful request still yields age ~0 at capture, and so does a
retry that eventually succeeds. The tolerance bites only when the newest
usable observation pre-dates the current attempt entirely — a refresh that
failed outright, or a book that vanished from an otherwise successful
response. Under today's one-refresh-per-checkpoint choreography that
fallback is usually hours old, so the tolerance is close to inert; it
becomes load-bearing when a pre-capture refresh cadence exists. See
`docs/phase4a4-threshold-recommendation.md` §2. The seven scenarios in `test_checkpoint_cycle.py` (A–G) are the
evidence a recommendation has to be argued from, and F/G exist to prove
the two clocks stay independent — F is a late scheduler with fresh quotes,
G is an on-time scheduler with stale ones.

---

### 3.5 Coverage degradation is shown to competitors

`number_of_books: 3` is ambiguous: three books existed, or five existed
and two were refused as stale. Those are *degraded coverage* and
*naturally thin coverage*, and presenting the first as the second presents
a feed problem as a market fact.

The shared evidence payload and the request `MarketContext` therefore
carry:

```
market_context:
    number_of_books
    books_observed
    stale_books_excluded
    canonical_quote_stale
```

Deliberately **not** exposed: the stale quotes' prices, which book they
came from, and the vendor's `last_update`. A competitor learns the
evidence was degraded and by how much; it does not get the rejected prices
back through a side door, and it never receives a field the freshness rule
is itself forbidden to use.

All three competitors receive identical metadata, so this remains shared
evidence.

This changes what every competitor is shown, so `BENCHMARK_PROMPT_VERSION`
moves **benchmark-v2 → benchmark-v3**, and the instruction text explains
what the three counts mean. `FORECAST_SCHEMA_VERSION` stays at
**forecast-v1**: the response schema is untouched, and bumping it would
falsely invalidate every stored forecast's shape contract. The renderer
refuses a request carrying the old prompt version, so a season already
running under v2 cannot silently acquire v3's fields mid-season.

The bare-integer lesson from v1→v2 applies directly: three providers all
independently misread `confidence` when its meaning lived only in a schema
keyword. `books_observed` would be read the same way, so its semantics are
in the instruction, not in the field name.

---

### 3.6 Tolerance configuration

```
None          gate disabled (the default; Phase-2 behaviour)
integer >= 0  explicit threshold, in seconds
negative      configuration error, rejected
```

A negative tolerance is not a strict rule. It marks every observation
stale, so every snapshot loses its canonical baseline and the run reports
a total market outage that never happened. It is rejected in three places:
at CLI parse, at `MarketSnapshotService` construction (before a run starts
rather than partway through a slate), and by a DB CHECK. `bool` is
rejected too — `True` is an `int` in Python and would become a one-second
tolerance.

`run_checkpoint_cycle` validates it before calling `refresh`, so a
misconfiguration cannot spend a provider call before failing.

### 3.7 The capture policy is frozen, not chosen per run

`SeasonRules` carries both `max_observation_age_seconds` and
`refresh_retry_policy`. Both materially change **which market state may
reach a checkpoint**, and a capture is irreversible — letting an operator
pick either per run would mean two checkpoints in the same season were
built under different rules with nothing in the record saying so.

`CapturePolicy.from_season_rules()` is the only official constructor. It
carries the row's `rules_version`, so a cycle report distinguishes "the
season's rules said so" from "someone injected a value for this run"; a
directly-constructed policy (calibration, tests) reports
`rules_version = None` and `is_official = False`.

NULL on either column means no capture policy was frozen — which is what
every pre-4A.5 season genuinely ran under. It is not a zero-second
tolerance and not zero attempts.

**`from_season_rules` fails closed on NULL for a real-provider season.**
The first version did not, and the result was a masquerade: a real season
row with both fields NULL carries a `rules_version`, so the policy
reported `is_official = True` while actually meaning *no freshness gate,
one attempt*. That made the absence of a decision indistinguishable from a
decision to disable the gate, and it did so on the run that looked most
authoritative. A real-provider season now raises `CapturePolicyNotFrozen`
until both fields are set.

`is_official` likewise means more than "a rules_version exists": every
capture-policy value must have come from the frozen row, tracked by a
`retry_frozen` flag so "the rules say one attempt" and "no retry policy
was ever frozen" are not the same object.

Synthetic seasons still resolve to an ungated policy — they fabricate
their own market data and have no provider to overspend against — but
report `is_official = False`, so they can never be mistaken for a frozen
research contract.

**The frozen JSON is strictly validated, never coerced.** `int("3")`,
`int(True)` and `int(3.7)` all succeed in Python. Unknown keys are
rejected too, since a misspelled field would silently take its default.
These rules are the research contract; a typo in them must fail at load
time, not quietly change how many times we are willing to pay.

---

### 3.8 One worker per eligible cycle

The read-only preflight stops a scheduler paying twice IN SEQUENCE. It
cannot stop two workers racing:

```
worker A: preflight -> ELIGIBLE
worker B: preflight -> ELIGIBLE
worker A: pays for refresh
worker B: pays for refresh
worker A: captures
worker B: finds CAPTURED
```

Both paid, for a checkpoint only one could capture. `checkpoint_lease.py`
makes the claim atomic: one `INSERT ... ON CONFLICT DO UPDATE ... WHERE`,
so two concurrent workers cannot both win.

- **The claim commits before any network work.** No transaction and no row
  lock is held across the provider call or the retry backoff.
  `SELECT FOR UPDATE` is unusable here for that reason.
- **The lease spans the refresh AND the capture.** Releasing after the
  refresh leaves a window in which a worker has finished paying but has
  not yet captured — and a second worker claiming in that window
  re-verifies ELIGIBLE and pays again. (Found by the race test, not by
  reading the code.)
- **The disposition is re-verified under the lease.** The preflight read is
  unsynchronized and can be stale by the time the claim succeeds.
- **A crashed worker cannot block the checkpoint.** The lease expires and
  the next worker reclaims it, which is why the claim is an upsert guarded
  on expiry rather than a bare INSERT. A takeover takes a fresh row id, so
  the overrunning worker's release cannot delete the new holder's lease.
- **A worker that loses the lease captures nothing either.** It has no
  fresh data and the holder is mid-refresh; capturing would freeze a
  snapshot built on pre-refresh state and consume the checkpoint out from
  under them.

---

### 3.9a The official runner always refreshes

`official_capture` resolves game → season → active rules → policy **and the
production refresh for that season's pinned provider**. It never runs a
cycle with `refresh=None`.

The first version did. `run_checkpoint_cycle` treats `refresh is None` as
"no refresh supplied" and proceeds straight to the capture, so the
official command would have consumed a real, irreversible checkpoint using
whatever stale quotes happened to be sitting in Postgres. The freshness
gate would have invalidated them — and the checkpoint would still be
CAPTURED and gone.

A season pinned to a provider with no production refresh now raises
`NoProductionRefresh` and captures nothing. Adding a provider means adding
its refresh to `REFRESH_BUILDERS`, not relaxing the check.

`refresh_game_market_data` takes only a `game_id`: `Game.external_ref`
already holds the provider event identity permanently, so there is no
`--event-id` for an operator to get wrong, and no `list_events` call to
rediscover something we already know. Two provider calls per logical
attempt — the nflverse roster and the Odds event odds — which is why
`provider_calls_spent` sums what each attempt actually reports rather than
counting one per attempt.

The identity-resolution and quote-persistence loop is SHARED with
`live_ingest` (`persist_resolved_quotes`), not copied. A second
implementation could drift — different alias handling, a different
quarantine rule — and nothing would flag it, because both would keep
producing plausible rows. `live_ingest` is now a thin acceptance CLI over
the shared service.

The refresh builds no `MarketSnapshot`, no `EvidenceSnapshot`, and calls
no model; its job ends at committed quotes. A structural test asserts it.

---

### 3.9 The window guard covers the FIRST paid attempt

"ELIGIBLE at this instant" is not "there is enough window left to sensibly
begin network work". A cycle starting seconds before `window_end` can buy
a refresh and then cross the boundary, so the capture it paid for is
marked MISSED.

One rule, `_has_room`, used before attempt 1 and before every retry — two
separate guards would inevitably disagree about how much room a paid
attempt needs.

The size comes from the real call sequence, not a guess:

| | timeout |
| --- | --- |
| nflverse roster download | 60s |
| The Odds API `list_events` | 30s |
| The Odds API `fetch_quotes` | 30s |
| **request budget (default)** | **180s** |
| capture reserve (`window_guard_seconds`) | 60s |
| **room needed before any paid attempt** | **240s** |

A 60s guard alone would have covered only the odds timeout and let a cycle
start work it could not finish.

---

## 4. Observation retention

Repeated unchanged observations are **retained**. Two polls four hours apart
produce two `PropQuote` rows even when the line, both prices, and the vendor's
`last_update` are all identical.

This is deliberate and was chosen over state-change suppression. Consider:

```
10:00 — DK 74.5 / -115 / -105 observed
14:00 — DK 74.5 / -115 / -105 observed again
```

Retaining only the 10:00 row makes "the market was genuinely unchanged and freshly
observed" indistinguishable from "our feed stopped seeing that book at 10:04."
Those are different facts, and the difference is a checkpoint-freshness question,
which is exactly the kind of thing this project exists to measure. Storage is
cheap relative to methodological ambiguity.

No separate observations table. One table, `prop_quotes`, where every row is a
real observation.

---

## 5. Idempotency and provenance

Idempotency is a question of *provenance*, not of timestamp comparison. Every
quote is traceable to the exact successful provider response that produced it.

```
PropQuote
   ↓ provider_call_id
provider_calls
   ↓ raw response + SHA-256 + quota telemetry + timestamps
   ↓ ingestion_run_id
ingestion_runs
```

`PropQuote.provider_call_id` → `provider_calls.id`, NOT NULL.

```
fingerprint = sha256(
    provider_call_id,
    external_event_id,
    player identity,
    stat_family,
    sportsbook,
    line,
    over_price,
    under_price,
    parser_version,
)
```

`UNIQUE` on `fingerprint`. Insert-if-not-exists; a conflict is a retry, counted
in `ingestion_runs.quotes_deduplicated`, writing no row and raising no error.

Consequences, both intended:

- Reprocessing the same stored raw response is idempotent, because it reuses the
  original `provider_call_id`.
- A genuinely new poll is a new `provider_call_id` and therefore legitimately
  produces another observation, even when the odds have not moved. This is what
  makes §4 work.

Line movement is the ordered `PropQuote` sequence for a given
`(market, sportsbook, source)`.

---

## 6. Parser versioning and corrections

`PropQuote` stores `parser_version` and includes it in the fingerprint (§5).

Reprocessing an archived response under a corrected parser version:

- MUST reuse the original `provider_call_id`
- produces new immutable `PropQuote` rows
- does NOT overwrite or delete the prior rows
- remains idempotent within that parser version

**Phase 4 does NOT globally filter quote selection by "current parser version."**

A global filter would let a parser deployment silently erase historical coverage:
if v1 ingested Weeks 1–4 and v2 ships in Week 5, filtering to v2 makes Weeks 1–4
vanish unless every archived v1 response has already been replayed. A bug fix
must never be able to delete history.

Before parser-correction replay is used in production, an explicit supersession /
correction-selection rule MUST be defined so that a new parser deployment cannot
make older unreprocessed history disappear. Candidate shapes, neither chosen:

- the corrected row carries `supersedes_quote_id`, or
- a separate normalization record establishes which parse of a `provider_call` is
  authoritative

Until that policy exists, `parser_version` is provenance and audit metadata. It
is not a visibility switch.

---

## 7. Module layout

Mirrors `app/ai/` deliberately, so the two provider boundaries read identically.

```
app/marketdata/
  base.py               MarketDataProvider Protocol, ProviderFetchResult,
                        MarketDataError, error categories
  dto.py                normalized provider DTOs (§9)
  mapping.py            vendor market key -> internal stat family (§10)
  registry.py           provider name -> adapter instance
  ingestion.py          IngestionService — the only writer of Game / Player /
                        PropMarket / PropQuote from external data
  telemetry.py          quota and audit record construction (§11)
  providers/
    the_odds_api.py     the ONLY module that may know vendor JSON exists
    mock.py             deterministic fixture provider for tests
  validation_probe.py   the probe CLI (§14), sibling of app/ai/live_smoke.py
```

**Enforced by review and by test:** `providers/the_odds_api.py` is the only file
permitted to import `httpx` or reference a vendor field name. Everything above it
sees DTOs.

This applies to DIAGNOSTIC code as well as to the ingestion path. The validation
probe needs to inspect a payload shape we have not committed to parsing, but that
inspection lives in the adapter as `discover_event_shape()` and returns a neutral
`ProviderShapeReport`; the CLI never indexes into vendor JSON. Exempting
"it's only diagnostics" is how schema leakage starts, so a unit test asserts the
probe module contains no vendor field names.

---

## 8. `MarketDataProvider` interface

Capability-named, not endpoint-named, so a differently shaped provider still fits.

```python
class MarketDataProvider(Protocol):
    provider_name: str
    supports_historical: bool

    def list_events(
        self, *, sport: str, window_start: datetime, window_end: datetime
    ) -> ProviderFetchResult[list[ProviderEvent]]: ...

    def fetch_quotes(
        self, *, event: ProviderEventRef, stat_families: Sequence[StatFamily],
        books: Sequence[str] | None = None,
    ) -> ProviderFetchResult[list[ProviderQuote]]: ...

    def fetch_quotes_as_of(
        self, *, event: ProviderEventRef, stat_families: Sequence[StatFamily],
        as_of: datetime, books: Sequence[str] | None = None,
    ) -> ProviderFetchResult[list[ProviderQuote]]: ...
```

Design notes:

- Callers pass **internal** `StatFamily` values. The adapter translates.
- `books=None` means every book the provider carries. Requesting DraftKings
  exclusively would destroy the `number_of_books`, median, min, and max fields
  `MarketSnapshot` already computes. DraftKings is selected as canonical
  *downstream*, not requested exclusively upstream.
- `fetch_quotes_as_of` is a separate method rather than an optional `as_of=`
  parameter because it has different cost (10×), a different access tier (paid
  only), and different failure modes. Hiding a 10× billing difference behind a
  default argument is how surprise invoices happen.
- `supports_historical` lets the ingestion service refuse a backfill up front
  rather than discovering it as a 4xx.
- `ProviderFetchResult[T]` mirrors Phase 3's `ProviderCallResult`: exactly one of
  `(payload, error)` is meaningful, telemetry populated either way. The
  catch-all backstop pattern from `app/ai/orchestrator.py:_invoke_adapter`
  applies here unchanged, so a gap in adapter error handling can never strand an
  `ingestion_run` at `RUNNING`.

---

## 9. Normalized DTOs

Frozen dataclasses. `Decimal` for lines, `int` for American prices, matching
`PropQuote`'s `Numeric(6,2)` / `Integer` exactly so no lossy float enters the
pipeline.

```python
@dataclass(frozen=True)
class ProviderEventRef:
    provider: str
    external_event_id: str

@dataclass(frozen=True)
class ProviderEvent:
    ref: ProviderEventRef
    sport_key: str
    kickoff_at: datetime              # tz-aware UTC
    home_team: str
    away_team: str

@dataclass(frozen=True)
class ProviderPlayerRef:
    provider: str
    external_player_id: str | None    # None if the vendor exposes no stable id
    display_name: str                 # exactly as the vendor returned it
    team: str | None
    position: str | None

@dataclass(frozen=True)
class ProviderQuote:
    event: ProviderEventRef
    player: ProviderPlayerRef
    stat_family: StatFamily           # internal enum, already mapped
    vendor_market_key: str            # audit and diagnosis only
    sportsbook: str                   # normalized to our uppercase form
    line: Decimal
    over_price: int
    under_price: int
    as_of_at: datetime
    retrieved_at: datetime
    provider_market_updated_at: datetime | None
    source: str                       # provider_name
```

Deliberate exclusions: no raw blob on the DTO, no vendor response object, no URL.
Raw preservation happens at the call level (§11), never smuggled per-quote.

Over/under pairing happens **in the adapter**. The vendor returns Over and Under
as separate outcome entries; a `ProviderQuote` is emitted only when both sides are
present at the same line. An unpaired side is `INCOMPLETE_PRICE_PAIR` (§13), never
a quote with a null price.

`vendor_market_key` rides along for diagnosis. It is never part of any identity,
join, or downstream branch.

---

## 10. Market mapping

One frozen, explicit table. No pattern matching, no prefix heuristics, no
fallback.

```python
VENDOR_MARKET_KEYS: Mapping[str, StatFamily] = {
    "player_pass_yds":      StatFamily.PASSING_YARDS,
    "player_pass_tds":      StatFamily.PASSING_TOUCHDOWNS,
    "player_rush_yds":      StatFamily.RUSHING_YARDS,
    "player_receptions":    StatFamily.RECEPTIONS,
    "player_reception_yds": StatFamily.RECEIVING_YARDS,
}
```

> **All five spellings are now OBSERVED.** The 2026-09-17 validation probe
> requested them against a real NFL event and the vendor returned all five
> verbatim, with DraftKings quoting every one of the five internal families.
> Promoting them into `VERIFIED_MARKET_KEYS` is authorised and is Phase 4A.2
> item 1; the table is left empty here so that promotion is a reviewed,
> deliberate commit rather than a side effect of this document changing.
>
> The same docs show alternate NFL player props under **separate `_alternate`
> market keys**, which supports the V1 rule below: the mapping simply does not
> list them, so alternates never enter. The ingestion-level ambiguity quarantine
> in §10.1 stays as defence in depth — it costs nothing when alternates are
> separately keyed, and it is the only thing standing between us and an
> arbitrary canonical baseline if that ever changes.

**Quarantine, never coercion.** An unrecognized vendor key is never mapped to a
nearby family. It is counted in `ingestion_runs.markets_quarantined`, its key
recorded, and it produces no `PropQuote`. Silently coercing
`player_pass_attempts` into `passing_yards` is the class of bug that poisons a
season of research without ever failing a test.

Direction is one-way: vendor → internal. Nothing downstream translates back.

### 10.1 Alternate lines

After mapping, if more than one candidate line exists for the same
`(provider_call, sportsbook, player, stat_family)` and vendor semantics do not
*prove* which is the standard line, **quarantine the entire ambiguous set**. Emit
no research `PropQuote` for that book / player / stat from that response.

Never select the first row. `MarketSnapshotService` currently resolves one quote
per book by taking the first row per `sportsbook` in the ordered result:

```python
latest_by_book: dict[str, BookQuote] = {}
for q in quote_rows:
    if q.sportsbook not in latest_by_book:
        latest_by_book[q.sportsbook] = BookQuote(...)
```

With simultaneous alternates present, "first" is arbitrary ordering — and it
silently determines the canonical baseline and therefore every de-vigged
probability the competitors see. Sacrificing one book's quote for one observation
is strictly better than an unstable baseline.

Never select by heuristic, and specifically never "closest to -110," which would
let price noise reassign the canonical line.

The probe may make this moot: it is plausible that alternates are separately
keyed, in which case §10's table simply omits them and they never enter. That MUST
be proven from a real payload, not assumed.

---

## 11. Telemetry and audit

Per-response, not per-run. Two new tables.

**`ingestion_runs`** — one row per logical ingestion operation.

```
id, provider, operation          LIST_EVENTS / FETCH_QUOTES / FETCH_HISTORICAL
sport, season_id, week_number
requested_stat_families
checkpoint_run_id                nullable FK
status                           PENDING / RUNNING / SUCCEEDED / PARTIAL / FAILED
started_at, finished_at
events_seen, quotes_observed, quotes_written, quotes_deduplicated
markets_quarantined
```

**`provider_calls`** — one row per HTTP response. This is the telemetry.

```
id, ingestion_run_id FK
endpoint_capability              our capability name, never a vendor path or URL
requested_at, responded_at
http_status, success
error_category                   nullable, §13
quota_used                       x-requests-used
quota_remaining                  x-requests-remaining
quota_cost                       x-requests-last
provider_request_id              nullable
provider_snapshot_at             nullable; supplied by historical endpoints
raw_response_body                full, sanitized (§11.1)
raw_response_sha256
raw_response_bytes
```

`provider_calls` is where the money lives. One row per response answers "what did
Week 0 actually cost" by summing `quota_cost`, and sets the reserve threshold from
the newest `quota_remaining` rather than from an estimate.

**Never persisted:** the API key, any `Authorization` header, any URL containing
`apiKey=`. The adapter builds URLs; `provider_calls` stores our capability name
and never a URL. Same rule Phase 3 already follows.

### 11.1 Raw response preservation

Full sanitized response body plus SHA-256, retained for Season 1.

A hash alone proves a response you still possess is unchanged; it cannot
reconstruct a normalization you got wrong or diagnose a parsing mistake after the
fact. For a reproducibility-first research project that is the wrong trade.

"Sanitized" means: we store the response *body* and our capability name. We never
store the request URL, which is where the key travels.

Week 0 MUST report total bytes so the object-storage decision is made on
measurement rather than intuition. If volume justifies it, blobs move to object
storage later with the hash and reference retained in Postgres.

### 11.2 Credit reserve

`MINIMUM_CREDIT_RESERVE = 5000`, applied to historical and backfill calls only.
The free-tier current-data probe is exempt.

Before any `fetch_quotes_as_of` call, the service reads the most recent
`quota_remaining`; below reserve, the run aborts with `QUOTA_RESERVE_EXHAUSTED`
rather than spending.

There is **no automatic checkpoint bypass.** Spending into the reserve requires an
explicit operator flag, recorded on the `ingestion_run`, so cost protection cannot
silently disappear through a future code path.

---

## 12. Ingestion boundary

```
external provider
    ↓
provider adapter          (the only layer that knows vendor JSON exists)
    ↓
normalized provider DTOs
    ↓
IngestionService          (owns ALL mapping into our persistence conventions)
    ↓
Game / Player / PropMarket / immutable PropQuote
    ↓
MarketSnapshotService     (unchanged; reads persisted rows, never fetches)
    ↓
EvidenceSnapshot / Forecast Lab
```

The `IngestionService`, not the adapter, owns mapping into internal persistence
and domain conventions. The adapter's only job is vendor → DTO.

Transaction discipline follows Phase 3: **no database transaction spans a network
call.** Fetch, close the call, then persist in its own unit of work.

### 12.1 Identity

**Event.** `Game.external_ref` is already `UNIQUE NOT NULL`. Store
`f"{provider}:{external_event_id}"` rather than the bare vendor id, so a second
provider cannot collide. Get-or-create.

**Prop market.** `(game_id, player_id, stat_type)` is the natural key. Add a
`UNIQUE` constraint — there is none today. Get-or-create.

**Player.** See §12.2. OPEN.

### 12.2 Player, team, and position

A `Player` is a person. A player's team is time- and game-dependent, so
`Player.team` as a single mutable global is the wrong shape for historical
research: a midseason trade silently rewrites every prior week's market context.

Accepted target shape:

```
Player       stable person identity
GamePlayer   (game_id, player_id) UNIQUE
             + team, + position (nullable), + source, + observed_at
PropMarket   game + player + stat_family
```

`Player.team` is retained through migration for compatibility but becomes
explicitly **non-authoritative and deprecated** for historical research.

`GamePlayer` MUST NOT be built before the probe. `Player.team` and
`Player.position` are both `NOT NULL` today, so if the vendor supplies neither,
we cannot honestly write a `Player` row at all and production Week 0 ingestion is
blocked pending either a roster source or a schema decision.

**Placeholder values such as `"UNKNOWN"` are prohibited.** This is a prohibition,
not a preference. Fabricating roster data to satisfy a `NOT NULL` constraint
would corrupt the research record in a way that is invisible at query time.

If no stable player id exists, the fallback external ref is
`f"{provider}:name:{normalized_name}"`, where normalization is limited to:

- Unicode NFKC
- case folding
- whitespace collapse

**Nothing else.** Suffixes — Jr., Sr., II, III — are NEVER stripped: the NFL has
live father/son and same-family cases, and stripping them collapses distinct
people. No fuzzy matching, ever. Player reconciliation is explicit, named
technical debt, not something to paper over with string similarity.

Record which scheme produced each ref so a later migration can tell them apart.

### 12.3 Source pinning

`SeasonRules.market_data_provider` (e.g. `"THE_ODDS_API"`), frozen and versioned
alongside `canonical_sportsbook`.

`quotes_as_of` gains a required `source` parameter; `MarketSnapshotService` passes
`SeasonRules.market_data_provider`. Mixing DraftKings-from-provider-A with
FanDuel-from-provider-B becomes impossible rather than merely discouraged, and
switching vendors midseason cannot silently change the research baseline.

The migration backfills existing rows with a synthetic source that cannot collide
with a real provider name.

---

## 13. Failure model

Normalized, closed set, mirroring `app/ai/providers/base.py`. No
provider-specific branching above the adapter.

**Transport / access** — no usable data at all:

```
AUTHENTICATION_ERROR
QUOTA_EXHAUSTED             provider says credits are gone
QUOTA_RESERVE_EXHAUSTED     our own pre-flight reserve guard tripped
RATE_LIMITED
TIMEOUT
PROVIDER_UNAVAILABLE        DNS / TLS / connection / 5xx
UNKNOWN_PROVIDER_ERROR      catch-all backstop
```

**Content** — provider responded, data unusable:

```
MALFORMED_RESPONSE
UNSUPPORTED_MARKET          vendor key not in our mapping -> quarantine
AMBIGUOUS_ALTERNATE_LINE    multiple candidate lines, standard not provable (§10.1)
MISSING_CANONICAL_BOOK      DRAFTKINGS absent for this market
INCOMPLETE_PRICE_PAIR       over without under, or mismatched lines
STALE_HISTORICAL_SNAPSHOT   returned snapshot too far from the requested as-of
```

Semantics, which is the part that protects the ledger:

- Failures are recorded on `provider_calls` and `ingestion_runs`. They NEVER
  write, modify, or delete a `PropQuote`. `PropQuote` remains strictly
  insert-only.
- A run that ingests some markets and quarantines others is `PARTIAL`, not
  `FAILED`, and successfully normalized quotes are still written. Partial success
  is the normal case; no run is all-or-nothing at the quote level.
- `MISSING_CANONICAL_BOOK`, `INCOMPLETE_PRICE_PAIR`, and
  `AMBIGUOUS_ALTERNATE_LINE` are per-market, not per-run. Other books' quotes for
  that market still persist, and `MarketSnapshot` already handles a null canonical
  baseline via `is_valid_canonical_baseline`.

---

## 14. Historical reconstruction — Phase 4B

A separate entry point. It does NOT call `capture_checkpoint` (§2.2).

It uses `CheckpointRun.target_time` as the locked as-of and the provider snapshot
nearest to it within a configured tolerance. Outside tolerance is
`STALE_HISTORICAL_SNAPSHOT`, and the checkpoint stays unreconstructed rather than
being filled with an approximation.

Live checkpoint rules are unchanged. The `MISSED` branch is not weakened.

---

## 15. Provider-validation probe

```
python -m app.marketdata.validation_probe
```

Built like `app/ai/live_smoke.py`: a production-safe CLI, not pytest, not an HTTP
endpoint. Free tier. One event. Current data only. Roughly 5 credits
(markets × regions). **Persistence OFF by default** — the first run proves the
shape before it writes into research tables.

Runs from inside Railway via `railway ssh`. The development sandbox's egress proxy
blocks the vendor domain outright, so the adapter cannot be live-tested there.

Reads `THE_ODDS_API_KEY` from the environment; aborts clearly if absent; never
echoes it.

### Deliverables

Every one of these is a decision blocker. None may be guessed at in code.

1. The exact vendor market keys returned, verbatim (corrects §10).
2. Is `DRAFTKINGS` present, and which of the five families does it quote?
3. Player identity: a stable id, or a display name only? An identifier being
   PRESENT and an identifier MEANING player identity are separate claims; only
   an explicitly player-semantic field (`player_id`, `participant_id`) counts as
   stable. A bare `id` is reported as an unclassified identifier, because
   guessing its semantics would silently merge or split real people in
   `Player.external_ref`.
4. Player metadata: does the response carry team? position? (§12.2)
5. Alternate lines: a separate market key, or multiple outcomes within one key?
   (§10.1)
6. The market-level `last_update` printed alongside the sample normalized quote,
   proving the adapter binds it at the correct level (§3).
7. Quota headers: used, remaining, cost for this call.
8. Raw response size in bytes (§11.1).
9. One fully normalized `ProviderQuote`, printed as a DTO — meaning a matched
   Over/Under pair at a single line with both prices, not one outcome. If no
   valid pair exists, that MUST be reported as unavailable with the reason,
   never satisfied by presenting a single unpaired outcome.

Exits non-zero on failure with the normalized error category.

Historical data is paid-only, so the probe deliberately cannot exercise
`fetch_quotes_as_of`. That is the first thing to test after the tier upgrade, as
its own small paid smoke — not part of this.

---

## 16. Cost discipline

| Operation | Cost |
| --- | --- |
| `/sports`, `/events` | free |
| Current event odds | markets × regions — 5 credits per event call |
| Historical event odds | 10 × markets × regions, **per event** |

Reconstructing one week blind: 16 games × 5 families × 3 checkpoints × 1 region ×
10 = **2,400 credits**.

`benchmark_slate_size` is 10, but slate selection is deterministic and
*downstream*. Ingestion MUST sweep the full eligible universe first, or the
selection is not deterministic — it is merely biased by whatever we happened to
fetch. So 2,400/week is the honest ingestion figure and 10 is the slate figure.
They are not in tension, and 2,400 MUST NOT be read as steady-state benchmark
cost.

One unverified item: `/sports` and `/events` are confirmed free, but whether the
*historical* events endpoint is free was not confirmed, and backfill needs it to
resolve event ids. Small money at 16 games × 3 checkpoints, but the first
backfill's `provider_calls.quota_cost` answers it exactly — another reason
telemetry is per-response.

---

## 17. Schema delta

```
PropQuote    + as_of_at                    NOT NULL
             + source                      NOT NULL
             + provider_call_id            FK NOT NULL
             + ingestion_run_id            FK
             + provider_market_updated_at  nullable
             + parser_version
             + fingerprint                 CHAR(64) UNIQUE
             (NO composite UNIQUE on market/book/source/as_of — see §10.1)

PropMarket   + UNIQUE(game_id, player_id, stat_type)

SeasonRules  + market_data_provider

new tables   ingestion_runs
             provider_calls        (raw body + sha256 + bytes + quota telemetry)

later        GamePlayer            (post-probe, §12.2)

4A.3         MarketSnapshot  + max_observation_age_seconds  nullable (threshold IN FORCE)
                             + stale_books_excluded         NOT NULL
                             + canonical_quote_stale        NOT NULL
                             + selected_quotes              JSONB (the frozen selection)
             Additive, no backfill. Pre-4A.3 rows keep NULL, which is the honest
             value -- no threshold was in force -- and is NOT the same as 0.

4A.4         MarketSnapshot  + books_observed               NOT NULL
             CHECK max_observation_age_seconds IS NULL OR >= 0
             CHECK books_observed = number_of_books + stale_books_excluded
             Backfilled to number_of_books + stale_books_excluded, which is true
             by definition for any row rather than only for rows written without
             a gate. The CHECK is added AFTER the backfill so it validates the
             existing rows instead of being taken on trust.

             MarketContext (request schema) + books_observed
                                            + stale_books_excluded
                                            + canonical_quote_stale
             BENCHMARK_PROMPT_VERSION benchmark-v2 -> benchmark-v3
             FORECAST_SCHEMA_VERSION unchanged (forecast-v1)

4A.5         SeasonRules  + max_observation_age_seconds  nullable
                          + refresh_retry_policy         JSONB nullable
             CHECK max_observation_age_seconds IS NULL OR >= 0
             Additive, NO VALUE SET on any row. NULL means no capture policy
             frozen, which is what every pre-4A.5 season ran under.

4A.5c        new table  checkpoint_cycle_leases
             UNIQUE(game_id, checkpoint_type) -- what ON CONFLICT targets
             CHECK expires_at > acquired_at

             (`provider_market_updated_at` was required by the time model in
             §3 and by the ProviderQuote DTO in §9 from the start; its absence
             from this summary was a documentation omission, corrected during
             Phase 4A.1 implementation rather than treated as a decision.)

migration    additive, per Phase 3 policy.
             Existing PropQuote rows get as_of_at = retrieved_at (correct: every
             quote written so far came from a synthetic current pull) and a
             synthetic source that cannot collide with a real provider.

touched      quotes_as_of                      filter/order on as_of_at, require source
             market_snapshot_service.py:35     comment + its tests
             checkpoint_service.py             left alone (§14)
```

---

## 18. Open items blocked on the probe

| Item | Section |
| --- | --- |
| Player identity, team, and position source | §12.2 |
| Alternate-line multiplicity shape | §10.1 |
| Vendor market key spellings | §10 |
| `last_update` binding level | §3 |
| Whether the historical events endpoint is free | §16 |

All MUST be answered from a real payload before production Week 0 ingestion.

Separately, and not blocked on the probe: the authoritative parser
supersession / correction-selection policy (§6) MUST be defined before
parser-correction replay is used in production.

Also not blocked on the probe, and opened by Phase 4A.3 (§3.2): whether a
stale-book exclusion should surface in the `EvidenceSnapshot` payload the
competitors are shown. It currently does not. The evidence payload is
prompt-versioned and frozen per season, so this is a competition decision
rather than an ingestion one, and it MUST be settled before a season runs
with the gate enabled.

---

## 19. Decision log

Decisions where an earlier proposal was overturned in review, recorded so the
reasoning is not lost.

| Decision | Outcome |
| --- | --- |
| State-change suppression of unchanged quotes | **Rejected.** Retain every observation (§4). Suppression makes "unchanged" indistinguishable from "feed stopped seeing the book." |
| `UNIQUE(market_id, sportsbook, source, as_of_at)` | **Rejected.** Assumes one line per book per snapshot, which alternate lines may violate (§10.1). |
| Derive the live freshness threshold from `provider_market_updated_at` | **Rejected.** That is market-change age, not observation age (§3.1). A quiet market re-fetched one second ago would be thrown out; a feed that went dark would read fresh until it moved. |
| Measure quote staleness against `CheckpointRun.target_time` | **Rejected.** `target_time` is scheduling intent. A late capture is still a real capture; measuring against intent reports the scheduler's lateness as market staleness (§3.2). |
| A default `max_observation_age_seconds` | **Deferred, deliberately.** No defensible number exists yet, so the gate is opt-in and off by default. The recorded threshold column exists so whatever number eventually lands in `SeasonRules` can never be retroactively rewritten (§3.2). |
| Re-derive a snapshot's quote selection on demand instead of storing it | **Rejected.** A Phase 4B backfill can insert an `as_of_at` earlier than a snapshot already taken, so the same query returns a different answer for the same snapshot (§3.2). |
| Substitute another sportsbook when the canonical book's quote is stale | **Rejected.** RULES.md §16. "Too old to trust" is a form of unavailable; the baseline goes invalid and `canonical_quote_stale` says which failure it was. |
| Calibrate thresholds by capturing the durable DET @ BUF FINAL | **Rejected.** `capture_checkpoint` is idempotent after CAPTURED, so there is no second attempt — an experimental threshold would permanently freeze experimental artifacts onto a real game (§3.4). |
| Let the calibration preview have its own copy of the selection rule | **Rejected.** A preview built on a copy is evidence about the copy. The rule is one pure function both paths call, with an AST guard asserting nothing else ordering-compares an age to a tolerance (§3.4). |
| Discover the tolerance from observed quote ages | **Rejected as a method.** In the intended workflow the refresh precedes the capture, so healthy ages are ~0 and every candidate scores identically. The threshold is a post-refresh-FAILURE grace period; only the failure scenarios speak to it (§3.4). |
| Abort the capture when the refresh fails | **Rejected.** A missing checkpoint is a hole in the research record; a stale-but-labelled one is information. The gate decides usability and the report says the refresh failed (§3.3). |
| Show competitors the rejected stale quotes | **Rejected.** They are told coverage was degraded and by how much, never what the degraded evidence said, and never `provider_market_updated_at` (§3.5). |
| Hide the exclusion from competitors entirely | **Rejected.** `number_of_books: 3` would then mean either three books existed or five did and two were refused — presenting a feed problem as a market fact (§3.5). |
| Refresh before establishing the checkpoint's disposition (as 4A.4 shipped) | **Rejected, and fixed.** A scheduler re-tick on a CAPTURED checkpoint paid for quotes that could never reach it, once per tick. The preflight now gates the provider call (§3.3). |
| Retry a failed refresh by calling the cycle again later | **Rejected.** `capture_checkpoint` is idempotent once CAPTURED, so the first call has already consumed the checkpoint. Retries happen before the capture or not at all (§3.3). |
| Put retries inside the provider adapter | **Rejected.** Three billed calls would look like one. Each attempt is its own `ProviderCall` row (§3.3). |
| Retry every failure category | **Rejected.** A bad key stays bad and an exhausted quota stays exhausted. `MALFORMED_RESPONSE` is the sharpest: the provider answered and we were billed, so a retry pays again for the same unusable payload (§3.3). |
| "900s covers a slow refresh plus a retry or two" | **Retracted.** `as_of_at` stamps at response receipt, so a slow success and a successful retry both age ~0. The tolerance only bites when the newest usable observation pre-dates the attempt entirely (§3.4). |
| Let an operator pass the tolerance or retry budget per official run | **Rejected.** Both change which market state reaches an irreversible capture, so both are frozen rules and a change is an amendment (§3.7). |
| Treat a NULL capture policy as "no gate, one attempt" | **Rejected, and fixed.** It reported `is_official = True` while meaning no decision had been made — the absence of a decision masquerading as an authoritative one (§3.7). |
| Rely on the read-only preflight alone for cost integrity | **Rejected.** It only serializes a single scheduler. Two workers both observe ELIGIBLE and both pay; the claim has to be atomic (§3.8). |
| Release the cycle lease after the refresh | **Rejected.** It leaves a window where a worker has paid but not captured, and a second worker claiming there re-verifies ELIGIBLE and pays again. The lease spans the capture (§3.8). |
| `SELECT FOR UPDATE` for the claim | **Rejected.** It would hold a lock across an unbounded provider wait (§3.8). |
| Coerce frozen retry-policy JSON with `int()`/`float()` | **Rejected.** `int("3")`, `int(True)` and `int(3.7)` all succeed, turning a typo in the research contract into a plausible-looking policy (§3.7). |
| Guard only retries against the window end | **Rejected.** Attempt 1 can be bought seconds before `window_end` and land as MISSED. One rule covers both, sized to the real call sequence (§3.9). |
| Let the official runner capture with no refresh wired | **Rejected, and fixed.** `refresh=None` means "proceed to the capture", so the official command would have consumed an irreversible checkpoint on stale quotes (§3.9a). |
| Require an operator `--event-id` for the production refresh | **Rejected.** `Game.external_ref` holds the event identity permanently; asking for it again invites a typo into a permanent decision, and `list_events` would spend a credit to rediscover it (§3.9a). |
| Copy the resolution loop into the production refresh | **Rejected.** Two implementations of identity resolution could drift from the one that wrote the existing research record, and both would keep producing plausible rows (§3.9a). |
| Count one ProviderCall per retry attempt | **Rejected.** One logical refresh is a roster call plus an odds call, so that understates what a retry costs (§3.9a). |
| Apply an amendment without re-checking the parent under a lock | **Rejected.** Two amendments racing would both supersede the same row and leave two active clones; the operator must also supersede the row they reviewed (§3.7). |
| `as_of_at` derived from vendor `last_update` | **Rejected.** Wrong granularity (market-level, not bookmaker-level) and, more importantly, the wrong meaning: a quote row records an observation, not the vendor's belief about market change (§3). |
| Timestamp-based retry idempotency | **Replaced** by `provider_call_id` + fingerprint (§5). Provenance beats inference. |
| Hash-only raw retention for successful runs | **Rejected.** A hash cannot reconstruct a bad normalization (§11.1). |
| Stripping name suffixes in fallback normalization | **Rejected.** Collapses distinct people (§12.2). |
| Keep first line, quarantine the rest | **Rejected.** "First" is arbitrary ordering. Quarantine the entire ambiguous set (§10.1). |
| `quotes_as_of` filters to active parser version | **Rejected.** A parser upgrade could silently erase unreprocessed history (§6). |
| `prop_quote_observations` table | **Dropped.** Unnecessary once every observation is retained in one table (§4). |
| Team on `PropMarket` | **Rejected.** Repeats the same roster fact across five props. `GamePlayer` instead (§12.2). |
