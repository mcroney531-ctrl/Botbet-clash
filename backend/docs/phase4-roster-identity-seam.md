# Phase 4A.2 — Roster / Player Identity Seam

**Status:** ACCEPTED after two review corrections. Frozen enough to build against.
**Scope:** the roster/identity provider boundary. No implementation exists yet.

Companion to [`phase4-ingestion-seam.md`](phase4-ingestion-seam.md). That document
governs *what market exists and at what price*. This one governs *who the human
is and which side of the game they are on*. BotBet Clash owns the canonical
identity and team model in between; neither provider defines it.

The Odds API validation probe (2026-09-17) resolved every market-side question
and left exactly one blocker: the vendor supplies a player **display name only** —
no stable identifier, no team, no position. This seam closes that gap.

Every claim below marked *measured* was validated against real nflverse data
downloaded during the design pass, not asserted.

---

## 1. Two findings that shaped this design

### 1.1 The `opponent` derivation is already broken (MUST FIX in 4A.2)

`app/ai/orchestrator.py:350`:

```python
opponent = game.away_team if player.team == game.home_team else game.home_team
```

A string equality between a roster team and a market team. Today both sides are
synthetic (`"KC"`) so it passes. With real data `game.home_team` is
`"Buffalo Bills"` (Odds API display name) and the roster team is `"BUF"`
(nflverse code). They are **never** equal, so the expression always returns
`game.home_team`.

Every player on the slate would be reported as facing the home team — including
the home team's own players. Roughly half the slate gets a wrong `opponent` in
the prompt every competitor sees. No crash, no failing test, wrong research data.

This is the concrete reason team canonicalization (§7) is load-bearing rather
than cosmetic. It is also evidence for §3's decision: a field described as
"non-authoritative but conveniently populated" eventually gets treated as
authoritative.

**Audit result:** across the entire non-test codebase there are exactly **two**
reads of `Player.team` — `orchestrator.py:350` and `:354` — and **zero** reads of
`Player.position`. The migration surface is very small.

### 1.2 Roster data lags the market

The probed event kicks off 2026-09-18 (week 3). The newest published *weekly*
roster file was week 2. At an `OPENING` checkpoint (144–96h pre-kickoff) the
game's own week will never be published yet.

*Measured* week-1 → week-2 churn among 2,495 players present in both weekly
snapshots: 11 changed team, 5 appeared, 467 dropped.

> **Wording discipline.** That is ~0.4% **observed week-to-week team churn among
> players present in both sampled weekly snapshots**. It is *not* the probability
> that a prop player is misassigned. Prop-relevant players are not distributed
> like the whole roster population, and the added/dropped rows complicate the
> denominator. Do not quote it as a risk figure.

§5 resolves this with the two distinct nflverse products rather than by treating
a week-2 file as week-3 state.

---

## 2. Validation already performed (no Odds API credits)

`github.com` release assets are **not** egress-blocked from the dev sandbox
(unlike `the-odds-api.com`), and nflverse requires no credential. All assets
return HTTP 200 to plain `curl`:

| Asset | Size |
| --- | --- |
| `rosters/roster_2026.csv` | 940 KB |
| `rosters/roster_2026.parquet` | 541 KB |
| `weekly_rosters/roster_weekly_2026.csv` | 1.75 MB |
| `weekly_rosters/roster_weekly_2026.parquet` | 620 KB |
| `players/players.csv` | 7.3 MB |

Confirmed columns in both roster products: `season`, `week`, `team`, `position`,
`full_name`, `gsis_id`, plus cross-source ids (`espn_id`, `sportradar_id`,
`pfr_id`, `sleeper_id`, `yahoo_id`, `pff_id`). 32 teams.

**Resolution against the probe's own event (DET @ BUF), restricted to the two
teams:**

| odds name | status | gsis_id | team | pos | opp |
| --- | --- | --- | --- | --- | --- |
| Jared Goff | RESOLVED | 00-0033106 | DET | QB | BUF |
| Teddy Bridgewater | RESOLVED | 00-0031237 | DET | QB | BUF |
| Joshua Dobbs | RESOLVED | 00-0033949 | DET | QB | BUF |
| Tyler Conklin | RESOLVED | 00-0034270 | DET | TE | BUF |
| Trent Sherfield | RESOLVED | 00-0034487 | BUF | WR | DET |
| Kyle Allen | RESOLVED | 00-0034577 | BUF | QB | DET |
| DJ Moore | RESOLVED | 00-0034827 | BUF | WR | DET |
| Josh Allen | RESOLVED | 00-0034857 | BUF | QB | DET |
| Greg Dortch | RESOLVED | 00-0035500 | BUF | WR | DET |
| Ty Johnson | RESOLVED | 00-0035537 | BUF | RB | DET |
| `"jared  GOFF"` | RESOLVED | 00-0033106 | DET | QB | BUF |
| `"Notareal Person"` | UNRESOLVED_PLAYER | — | — | — | — |
| `"Jared Goff Jr."` | UNRESOLVED_PLAYER | — | — | — | — |

Ten real players across **both** teams, not one easy quarterback. Casing and
whitespace variants resolve. `"Jared Goff Jr."` refuses rather than matching Goff
— the conservative direction, and the payoff for not stripping suffixes (§6).

Goff resolves identically from the **current** roster product: `00-0033106`,
`DET`, `QB`.

### 2.1 Why the two-team constraint is load-bearing — *measured*

| scope | duplicate normalized names (week 2) |
| --- | --- |
| league-wide | **4** |
| within DET + BUF | **0** |

The four: `justin jefferson` (CLE + MIN), `devonta smith` (CAR + PHI),
`byron young` (LA + PHI), `marcus harris` (KC + TEN). At least two are genuinely
prop-relevant. A global name match would fuse them into one `Player` row
permanently. The event's own two-team constraint reduces the collision set to
zero here.

### 2.2 `MISSING_STABLE_ID` is real

The current 2026 roster contains **1 row with no `gsis_id`** out of 2,968. The
failure category in §8 is an observed case, not a defensive hypothetical.

---

## 3. Stable internal player identity

```
Player.external_ref = "GSIS:<gsis_id>"      e.g. "GSIS:00-0033106"
```

nflverse **resolves** the identity; GSIS **is** the identity. Persisting
`NFLVERSE:<id>` would bake a vendor into the person, and swapping roster
providers later would orphan every `Player` row.

### 3.1 `Player` becomes the person record (constraint-loosening migration)

The earlier draft of this seam contained a contradiction: it declared
`MISSING_POSITION` non-blocking while leaving `Player.position` `NOT NULL`,
which makes it blocking. Resolved by loosening:

```
Player
    external_ref   NOT NULL, UNIQUE     "GSIS:<gsis_id>"
    name           NOT NULL             display attribute
    team           NULLABLE             deprecated, NON-AUTHORITATIVE
    position       NULLABLE             deprecated, NON-AUTHORITATIVE
```

This is **not** purely additive, and that is accepted deliberately. Retaining
fake or last-observed values in `Player.team` purely to satisfy a legacy
constraint leaves a trap — and §1.1 is direct evidence that such traps get
sprung. `GamePlayer` (§4) becomes authoritative for game-scoped team and
position.

Cross-source ids (`espn_id`, `sportradar_id`, `pfr_id`, …) are **not** persisted.
Every unused identifier column is one more thing to keep correct. Deferred until
a use exists.

---

## 4. `GamePlayer`

```
GamePlayer
    id
    game_id                  FK games            NOT NULL
    player_id                FK players          NOT NULL
    team                     CanonicalTeam       NOT NULL
    position                 String              NULLABLE
    roster_provider_call_id  FK provider_calls   NOT NULL
    roster_season            Integer             NOT NULL
    roster_week              Integer             NULLABLE
    roster_basis             String              NOT NULL
    observed_at              timestamptz         NOT NULL
    UNIQUE(game_id, player_id)
```

`roster_season` / `roster_week` are stored explicitly and separately from the
game's own week, because of §1.2: the snapshot that resolved a player may not
correspond to the game's week, and that must be visible in a query rather than
inferred.

`roster_basis` records **which §5 path** produced this row:
`CURRENT_CONTEMPORANEOUS` | `ARCHIVED_CONTEMPORANEOUS` | `HISTORICAL_RECONSTRUCTED`.

### 4.1 Opponent is derived, never stored — and never guessed

```python
if game_player.team == game.home_team_canonical:
    opponent = game.away_team_canonical
elif game_player.team == game.away_team_canonical:
    opponent = game.home_team_canonical
else:
    raise TeamGameMismatch(...)      # TEAM_GAME_MISMATCH
```

There is **no** `else means away team` shortcut. If a resolved player's team is
neither side of the game, that is corrupted context and it MUST stop request
construction rather than silently produce a plausible-looking opponent. That
shortcut is precisely the shape of the bug in §1.1.

---

## 5. Roster products and historical semantics

nflverse publishes **two distinct products**, and conflating them is a
methodological error:

| product | asset | meaning |
| --- | --- | --- |
| current | `rosters/roster_<season>.csv` | the latest roster state, refreshed on nflverse's own cadence |
| weekly | `weekly_rosters/roster_weekly_<season>.csv` | week-level historical roster data back to 2002 |

### 5.1 The policy

```
LIVE (OPENING / MID / FINAL)
    fetch the CURRENT season roster near the checkpoint
    retain exact raw bytes + sha256 + retrieved_at
    roster_basis = CURRENT_CONTEMPORANEOUS
    -> this IS contemporaneous roster evidence, because we captured it ourselves

HISTORICAL, where we hold our own archived snapshot from that period
    use our archived snapshot
    roster_basis = ARCHIVED_CONTEMPORANEOUS

HISTORICAL, before our archive begins
    use the nflverse weekly roster as reconstructed context
    roster_basis = HISTORICAL_RECONSTRUCTED
    -> explicitly NOT proven contemporaneous
```

This solves the week-3/`OPENING` problem without pretending a week-2 file is
week-3 state.

> **Provenance caution.** Today's nflverse weekly history is not, by itself,
> proof of what was available contemporaneously at an old checkpoint — whether
> those files are contemporaneous captures or retrospective backfills is an open
> question upstream. We therefore never *claim* contemporaneity we did not
> establish ourselves. The `roster_basis` column is what keeps that claim honest
> and queryable, rather than a footnote someone forgets.

The earlier draft's "use the most recent weekly file ≤ game week" is **rejected**
as the live path for exactly this reason.

### 5.2 Audit reuse

Reuse `ingestion_runs` and `provider_calls` unchanged. They are already
provider-agnostic — `provider`, `operation` and `endpoint_capability` are free
strings, and the raw-body + sha256 + bytes columns apply as-is. Inventing
parallel roster telemetry would duplicate the sanitizer, the reserve guard and
every audit query for no gain.

```
ingestion_runs.provider             = "NFLVERSE"
ingestion_runs.operation            = "FETCH_ROSTER"
provider_calls.endpoint_capability  = "FETCH_CURRENT_ROSTER" | "FETCH_WEEKLY_ROSTER"
```

Quota columns stay `NULL` — nflverse has no credit model. That is correct, not a
gap.

**Raw retention:** the season CSV is ~940 KB (current) / ~1.75 MB (weekly).
Simple raw retention is acceptable for V1. Do **not** build content-addressed
blob infrastructure yet. If identical files are fetched repeatedly, hash them and
expose the duplication in telemetry; optimising physical storage can wait for a
measurement that justifies it.

No database transaction may be held open across the download.

---

## 6. `RosterDataProvider`

```
app/rosterdata/
  base.py        RosterDataProvider Protocol, RosterFetchResult, RosterDataError
  dto.py         RosterEntry, RosterSnapshot
  resolution.py  the name -> identity resolver
  teams.py       CanonicalTeam + the two provider maps
  registry.py
  providers/
    nflverse.py  the ONLY module that knows nflverse column names
    mock.py
  validation_probe.py
```

A third top-level boundary, sibling to `app/ai/` and `app/marketdata/`.
Deliberately **not** folded into `TheOddsApiProvider` or `IngestionService`.

```python
class RosterDataProvider(Protocol):
    provider_name: str

    def fetch_current_roster(
        self, *, season: int
    ) -> RosterFetchResult[RosterSnapshot]: ...

    def fetch_weekly_roster(
        self, *, season: int, week: int
    ) -> RosterFetchResult[RosterSnapshot]: ...
```

Two explicit methods rather than one `fetch_roster(season, week)` whose meaning
changes with its arguments. The live/historical distinction carries different
evidentiary weight (§5), so a future caller must not be able to reach for the
retrospective dataset by accident — the same reasoning that split
`fetch_quotes` from `fetch_quotes_as_of` in the market seam.

```python
@dataclass(frozen=True)
class RosterEntry:
    stable_id: str            # GSIS
    display_name: str
    team: CanonicalTeam       # internal enum, never a vendor string
    position: str | None
    season: int
    week: int | None

@dataclass(frozen=True)
class RosterSnapshot:
    provider: str
    season: int
    week: int | None
    basis: str                # CURRENT_CONTEMPORANEOUS | HISTORICAL_RECONSTRUCTED
    entries: tuple[RosterEntry, ...]
    retrieved_at: datetime
```

---

## 7. Resolution algorithm

Input: odds display name + `Game` (two canonical teams) + `RosterSnapshot`.

1. Normalize the odds name: **NFKC, casefold, whitespace collapse.** Nothing
   else. No suffix stripping, no punctuation heuristics, no fuzzy matching. (The
   same function the market DTO already uses.)
2. Candidate pool = snapshot entries whose canonical team is one of the game's
   two teams. **Never the whole league** (§2.1).
3. Exact match on normalized display name.
4. Exactly one → `RESOLVED`; zero → `UNRESOLVED_PLAYER`; more than one →
   `AMBIGUOUS_PLAYER`.
5. Resolved but roster team is neither canonical side → `TEAM_GAME_MISMATCH`.

Never create a `Player` from an unresolved odds name. Once this resolver exists,
the `THE_ODDS_API:name:<name>` fallback is **never** used for production
ingestion — it remains only as the probe's shape-discovery output.

**Alias table: deferred.** It would be justified by a measured miss rate and we
do not have one. Correct sequence: run 4A.2, let `UNRESOLVED_PLAYER` diagnostics
accumulate in telemetry, build an alias table only if the misses prove real and
repeating.

---

## 8. Team canonicalization

One internal vocabulary. Both providers map *into* it; neither defines it.

```python
class CanonicalTeam(StrEnum):   # 32 members, NFL standard abbreviations
    BUF = "BUF"
    DET = "DET"
    ...
```

Two explicit, exhaustive, hand-written tables:

```
ODDS_API_TEAM_NAMES   {"Buffalo Bills": CanonicalTeam.BUF, ...}
NFLVERSE_TEAM_CODES   {"BUF": CanonicalTeam.BUF, ...}
```

No substring matching, no fuzzy matching, no title-case heuristics. An unmapped
string is `TEAM_MAPPING_FAILED` and quarantines the market. A heuristic that maps
*Washington Commanders* correctly today will map something else wrongly later and
never tell us.

Both tables MUST be written from **observed** values, not from memory. nflverse
uses some non-obvious codes (`LA` rather than `LAR`, for instance); the real
32-code list is available from the downloaded file.

### 8.1 `Game` schema change

```
Game.home_team_canonical   CanonicalTeam   NOT NULL
Game.away_team_canonical   CanonicalTeam   NOT NULL
```

`home_team` / `away_team` are retained as the vendor display strings — useful for
the Show layer and for diagnosis. **All logic** moves to the canonical columns,
including §4.1.

**Migration safety:** map only explicit known values. On an unmapped value, fail
the migration and print the offending rows. Never guess, never default.

---

## 9. Failure model

**Transport / access**

```
ROSTER_SOURCE_UNAVAILABLE
ROSTER_TIMEOUT
UNKNOWN_ROSTER_ERROR
```

**Content**

```
MALFORMED_ROSTER_RESPONSE   header/schema not as expected
UNSUPPORTED_SEASON_WEEK     requested week not published (§1.2)
MISSING_STABLE_ID           roster row has no gsis_id — observed, see §2.2
UNRESOLVED_PLAYER           name not on either event team
AMBIGUOUS_PLAYER            more than one candidate
TEAM_GAME_MISMATCH          resolved team is neither canonical side
TEAM_MAPPING_FAILED         vendor team string not in the table
MISSING_POSITION            non-blocking
STALE_ROSTER_SNAPSHOT       snapshot older than configured tolerance
```

**Blocking rule.** Stable identity **and** a valid canonical game team MUST both
be established before a research `PropMarket` is created. `MISSING_POSITION`
alone does **not** block — `GamePlayer.position` is nullable and position is not
an input to any market math. Everything else quarantines that player's markets
for that call and never fabricates.

No roster failure may modify or delete existing `PropQuote` history — the same
rule the market seam applies.

---

## 10. Source pinning

```
SeasonRules.roster_data_provider = "NFLVERSE"
```

Frozen and versioned alongside `market_data_provider` and
`canonical_sportsbook`. Resolving Weeks 1–4 with one identity source and Week 5+
with another would silently change *who a player is* mid-season. Existing rows
backfill to `SYNTHETIC`.

---

## 11. Implementation source

**Direct nflverse-data release download, stdlib `csv`. Not `nfl_data_py`.**

```
https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_<season>.csv
https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_<season>.csv
```

Reasons, in order:

- **Zero new dependencies.** `nfl_data_py` pulls pandas + pyarrow, which this
  backend does not have and does not otherwise need.
- **We keep the exact raw bytes.** The whole provenance model is sha256 over what
  arrived on the wire; a DataFrame reader hands us its interpretation and the
  bytes are gone.
- Verified reachable from the dev sandbox with no credential and no rate limit.
- No coupling to an R package, and none to a Python wrapper's release cadence.

Parquet is smaller but needs pyarrow. CSV at ~940 KB is not worth a dependency.

**Accepted tradeoff:** we own the column contract. If nflverse renames a column
we break — mitigated by asserting the expected header on load and raising
`MALFORMED_ROSTER_RESPONSE`, never a silent `None`.

---

## 12. Migration plan

| Change | Kind |
| --- | --- |
| `Game.home_team_canonical`, `Game.away_team_canonical` NOT NULL | additive + backfill, fail loudly on unmapped |
| `SeasonRules.roster_data_provider` | additive, backfill `SYNTHETIC` |
| new table `game_players` | additive |
| `Player.team` → NULLABLE | **constraint-loosening** |
| `Player.position` → NULLABLE | **constraint-loosening** |
| `orchestrator.py:350` opponent derivation | **required code fix** (§1.1, §4.1) |

The loosening is safe: `NOT NULL` → `NULL` never rejects existing rows. The audit
in §1.1 found only two reads of these columns in the whole non-test codebase, and
both are in the code being rewritten anyway.

---

## 13. Open decisions

1. `STALE_ROSTER_SNAPSHOT` tolerance — how old may a current-roster snapshot be
   before a checkpoint refuses it?
2. Normalized `RosterEntry` mirror table, or rely on `roster_provider_call_id`
   provenance? Leaning **rely** — a mirror is a cache, not a source of truth, and
   it doubles the write path.
3. Whether the live path should also archive the current-roster snapshot on a
   schedule (independent of checkpoints), so that §5's
   `ARCHIVED_CONTEMPORANEOUS` path accumulates coverage from day one rather than
   only where a checkpoint happened to fire.

---

## 14. Phase 4A.2 gate

Not to be started until this document is accepted. Once accepted, 4A.2 may:

1. Promote the five observed market keys to `VERIFIED_MARKET_KEYS`.
2. Implement the real `TheOddsApiProvider.fetch_quotes` parser.
3. Implement `NflverseRosterProvider` + the resolver.
4. Add `CanonicalTeam`, the two team maps, and the `Game` canonical columns.
5. Add `GamePlayer`; loosen `Player.team` / `Player.position`.
6. **Fix the `opponent` derivation** (§1.1) — mandatory, not optional.
7. Enable current-data research persistence through `IngestionService`.
8. Build one real current `MarketSnapshot` from persisted real quotes.
9. Freeze the quote-freshness rule before any official Week-0 checkpoint.

Phase 4B (historical/backfill) remains out of scope and requires a separate
explicit go-ahead plus the paid tier.
