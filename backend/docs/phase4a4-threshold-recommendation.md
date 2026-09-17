# Freshness Tolerance — V1 Recommendation

**Status: PROPOSED. Nothing in the code carries this number.**

The gate is opt-in everywhere and defaults to `None`. `SeasonRules` has no
tolerance column. Both stay that way until this is accepted, because the
moment a number lands in frozen season rules it becomes part of the
research record and cannot be quietly revised.

---

## 1. What is being chosen

Not "how stale is a typical quote." In the Phase 4A.4 choreography the
refresh immediately precedes the capture:

```
refresh -> commit -> capture
```

so in a healthy cycle observation age is ~0 and **every candidate
threshold scores identically**. Scenario A demonstrates exactly that:
`max_observation_age_observed == 0.0`, no exclusions, and 900s / 3600s /
7200s are indistinguishable.

The number being chosen is a **post-failure grace period**: when the
refresh that should have preceded this capture did *not* work, how old are
we willing to let the last successful observation be before we stop
treating it as the market?

That reframing is the substantive result of this phase. A distribution of
quote ages sampled from arbitrary captures is not evidence about it.

---

## 2. The four inputs

**Refresh cadence.** One refresh per checkpoint, immediately before it.
There is no continuous poller — the Odds API free tier makes one
unaffordable, and Phase 4A.2 established that a capture is a discrete
event, not a tail of a stream. So a failed refresh means the last
observation is from the *previous* checkpoint, not from minutes ago. On
the real Week-3 data the two ingestion runs were **15h51m36s** apart.

This is the dominant input, and it points somewhere uncomfortable: with
one refresh per checkpoint, **any** tolerance below the inter-checkpoint
gap turns a single failed refresh into a lost checkpoint. There is no
value that both rejects genuinely stale prices and survives one failure,
because a failed refresh IS a genuinely stale price.

**Provider/network failure behaviour.** The Odds API failures we have
categorised are transport-level (timeout, 5xx, quota exhaustion). These
are retryable in seconds, not hours. A refresh failure that a retry cannot
fix is a sustained outage, and a sustained outage is precisely the case
where we should *not* be forecasting on last-known prices.

**FINAL-window sensitivity.** FINAL is kickoff−6h to kickoff−2h, targeted
at kickoff−3h. This is the window where lines move on injury news, and
where a three-hour-old price is not merely imprecise but systematically
wrong in a knowable direction. Tolerance should be tightest here.

**How long we will run on last-known observations.** This is the judgement
call, and it is a research-integrity question rather than a technical one.
A forecast scored against a canonical baseline that was three hours stale
is not a forecast about the same proposition the market was offering.

---

## 3. Recommendation

**One value: `max_observation_age_seconds = 900` (15 minutes).**

Not per-checkpoint-type. The argument for OPENING/MID tolerating more is
real — a line six days out moves slowly — but it buys nothing: with one
refresh per checkpoint, a failed OPENING refresh leaves an observation
days old, which no plausible per-type value rescues. Three numbers where
one suffices is three things to get wrong in a frozen rules row, and
RULES.md's bias is against complexity without demonstrated need.

Why 900 specifically:

- It comfortably covers a refresh that succeeded but ran slowly, plus a
  retry or two. Scenario B (600s-old observations, refresh failed) passes
  — the capture proceeds on last-known prices and says so.
- It is far below every plausible inter-checkpoint gap, so a genuinely
  failed refresh reliably invalidates the baseline rather than silently
  producing a stale one. Scenario C (3600s) fails, correctly.
- It is short enough that a FINAL capture cannot be built on a price from
  before an injury report.

**What 900 does NOT do, stated plainly:** it does not keep a checkpoint
alive through a refresh failure. It cannot — see §2. Its job is to make
the failure *visible and correctly labelled* (`canonical_quote_stale`,
`is_valid_canonical_baseline = false`, coverage counts in the evidence)
instead of producing a confident-looking snapshot built on yesterday's
market. If we want checkpoints to survive refresh failures, the fix is
retries and a second refresh attempt inside the window, not a looser
tolerance. That is a Phase 4A.5 question, not a number.

I would rather ship a tolerance that fails loudly than one chosen to make
the failure go away.

---

## 4. Proposed `SeasonRules` field

```python
# How old an OBSERVATION may be, at captured_at, and still count toward a
# snapshot. A post-refresh-failure grace period, not a measure of market
# movement -- see docs/phase4-ingestion-seam.md §3.2/§3.4.
#
# NULL means no gate, which is what every pre-4A.4 season ran under and is
# NOT the same as 0. Frozen alongside canonical_sportsbook and
# market_data_provider, and for the same reason: a midseason change would
# silently alter which observations the research baseline was built from.
max_observation_age_seconds: Mapped[int | None] = mapped_column(
    Integer, nullable=True
)

__table_args__ = (
    CheckConstraint(
        "max_observation_age_seconds IS NULL OR max_observation_age_seconds >= 0",
        name="max_observation_age_non_negative",
    ),
)
```

On acceptance:

1. Additive migration adding the column (nullable, no backfill — existing
   seasons genuinely ran with no gate).
2. `capture_checkpoint` and `run_checkpoint_cycle` resolve it from the
   active `SeasonRules` row rather than from a caller argument. The
   explicit parameter stays only for the calibration preview and tests.
3. An operator running an official capture cannot choose it ad hoc; a
   change is a rules amendment (a new `SeasonRules` row with
   `superseded_by`), never an UPDATE.

Until then the parameter stays caller-supplied and unset by default.

---

## 5. Evidence this rests on

`app/tests/integration/test_checkpoint_cycle.py`, scenarios A–G:

| | scenario | result |
| --- | --- | --- |
| A | refresh succeeds immediately before capture | ages 0.0; no candidate distinguishable |
| B | refresh fails, observations 600s old | captured, baseline valid, labelled |
| C | refresh fails, canonical 3600s old | baseline invalid, `canonical_quote_stale`, no substitution |
| D | one comparison book stale, canonical fresh | 1 excluded, baseline valid, context narrowed |
| E | no fresh comparison books | canonical-only snapshot, `books_observed` 4 vs `number_of_books` 1 |
| F | scheduler 3000s late, quotes fresh | large offset, ages 0.0 |
| G | scheduler on time, refresh failed earlier | offset 0.0, ages 5400 |

F and G are the pair that matters for the rule's shape: they produce
opposite readings from the same two fields, which is why
`scheduler_offset` is never an input to the gate.

Real-data anchor: the two live DET @ BUF runs, `04:19:20.012177Z` and
`20:10:56.225032Z`, 57096.2s apart.
