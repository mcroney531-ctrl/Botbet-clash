# Phase 4 — Real Market Data Ingestion Seam

**Status:** ACCEPTED. Frozen enough to build against.
**Scope:** the provider boundary only. No implementation exists yet.

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
`provider_market_updated_at`, and it MUST NOT drive checkpoint selection. The
vendor exposes `last_update` at the *market* level for event and player-prop
responses, not the bookmaker level; §14 requires the probe to prove our adapter
binds it at the correct level.

**Invariants.** `as_of_at <= retrieved_at`, always — a provider claiming a
snapshot from the future is a malformed response, not a quote. All timestamps are
timezone-aware UTC at the boundary; the adapter converts, and nothing downstream
sees a naive datetime or a vendor date string.

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

**Enforced by review:** `providers/the_odds_api.py` is the only file permitted to
import `httpx` or reference a vendor field name. Everything above it sees DTOs.

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

> **These five key spellings are UNVERIFIED.** Two of them (`player_pass_tds`,
> `player_rush_yds`) were confirmed against vendor documentation; the full set was
> not, because the vendor's docs are unreachable from the development sandbox.
> The probe (§14) MUST print the actual keys returned and this table MUST be
> corrected from a real payload before any paid call.

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
3. Player identity: a stable id, or a display name only?
4. Player metadata: does the response carry team? position? (§12.2)
5. Alternate lines: a separate market key, or multiple outcomes within one key?
   (§10.1)
6. The market-level `last_update` printed alongside the sample normalized quote,
   proving the adapter binds it at the correct level (§3).
7. Quota headers: used, remaining, cost for this call.
8. Raw response size in bytes (§11.1).
9. One fully normalized `ProviderQuote`, printed as a DTO.

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

---

## 19. Decision log

Decisions where an earlier proposal was overturned in review, recorded so the
reasoning is not lost.

| Decision | Outcome |
| --- | --- |
| State-change suppression of unchanged quotes | **Rejected.** Retain every observation (§4). Suppression makes "unchanged" indistinguishable from "feed stopped seeing the book." |
| `UNIQUE(market_id, sportsbook, source, as_of_at)` | **Rejected.** Assumes one line per book per snapshot, which alternate lines may violate (§10.1). |
| `as_of_at` derived from vendor `last_update` | **Rejected.** Wrong granularity (market-level, not bookmaker-level) and, more importantly, the wrong meaning: a quote row records an observation, not the vendor's belief about market change (§3). |
| Timestamp-based retry idempotency | **Replaced** by `provider_call_id` + fingerprint (§5). Provenance beats inference. |
| Hash-only raw retention for successful runs | **Rejected.** A hash cannot reconstruct a bad normalization (§11.1). |
| Stripping name suffixes in fallback normalization | **Rejected.** Collapses distinct people (§12.2). |
| Keep first line, quarantine the rest | **Rejected.** "First" is arbitrary ordering. Quarantine the entire ambiguous set (§10.1). |
| `quotes_as_of` filters to active parser version | **Rejected.** A parser upgrade could silently erase unreprocessed history (§6). |
| `prop_quote_observations` table | **Dropped.** Unnecessary once every observation is retained in one table (§4). |
| Team on `PropMarket` | **Rejected.** Repeats the same roster fact across five props. `GamePlayer` instead (§12.2). |
