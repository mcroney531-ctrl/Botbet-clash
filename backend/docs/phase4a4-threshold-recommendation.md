# Freshness Tolerance and Refresh-Failure Policy — V1 Recommendation

**Status: PROPOSED. No season carries these values.**

`SeasonRules.max_observation_age_seconds` and
`SeasonRules.refresh_retry_policy` exist as of Phase 4A.5 and are NULL on
every row. NULL means "no capture policy frozen", which is what every
season has genuinely run under. Assigning values to a season is a
deliberate provisioning decision, not a schema change.

> **Revision note (4A.5).** The previous version of this document claimed
> 900s "comfortably covers a refresh that succeeded but ran slowly, plus a
> retry or two." That was wrong and is retracted. See §3.

---

## 1. What is being chosen

Not "how stale is a typical quote." In the choreography the refresh
immediately precedes the capture:

```
preflight -> refresh (bounded retries) -> commit -> capture
```

so in a healthy cycle observation age is ~0 and **every candidate
threshold scores identically**. Scenario A demonstrates that directly.

The number being chosen is a **post-failure grace period**: when the
refresh that should have preceded this capture did not produce a usable
observation for a book, how old may that book's last successful
observation be before we stop treating it as the market?

---

## 2. The correction: when 900s actually bites

A current quote stamps `as_of_at` at **response receipt**
(`the_odds_api.py:316` — `observed = now or meta.responded_at or _utcnow()`).
Therefore:

- a **slow but successful** request produces a quote whose age is ~0 at
  capture, however long the HTTP call took;
- a **retry that eventually succeeds** likewise resets the age to ~0.

So the tolerance is not a network-timeout allowance and not a retry
allowance. It only matters when the newest usable observation **pre-dates
the current attempt entirely**:

1. the latest refresh failed outright and some earlier successful
   observation exists within the tolerance;
2. a particular book vanished from the latest (successful) response but
   was observed within the tolerance;
3. a future pre-capture refresh cadence deliberately creates a recent
   fallback.

**None of those regularly occur under today's one-refresh-per-checkpoint
choreography.** With one refresh per checkpoint, the previous successful
observation is from the previous checkpoint — the real Week-3 runs were
**15h51m36s** apart. So a full refresh failure leaves observations hours
old, and any tolerance from 0 up to the inter-checkpoint gap produces the
identical verdict.

Following that through honestly: **900s is currently close to inert.** It
discriminates only within a band that today's workflow never lands in.
Its practical effect right now is the same as a 0-second tolerance — make
a refresh failure loud.

That is not an argument against setting it. It is an argument that the
choice does not require precision yet, and that it becomes load-bearing
the moment case 2 or case 3 becomes real.

Scenario B in the test suite (600s-old fallback after a failure) is a
**policy scenario**, not an observed operational path. It documents what
the mechanism does when such a fallback exists; it is not evidence that
the current workflow produces one.

---

## 3. Recommendation: tolerance

**One value: `max_observation_age_seconds = 900` (15 minutes).**

Not per-checkpoint-type. The argument for OPENING/MID tolerating more is
real — a line six days out moves slowly — but it buys nothing: with one
refresh per checkpoint, a failed OPENING refresh leaves an observation
days old, which no plausible per-type value rescues. Three numbers where
one suffices is three things to get wrong in a frozen rules row.

Why 900 rather than 0, given §2 says they behave the same today:

- It is the right order of magnitude for case 2 (a book dropping out of
  one response) once any pre-capture cadence exists, and for case 3.
- It is short enough that a FINAL capture can never be built on a price
  from before an injury report.
- A 0-second tolerance would be actively wrong later and would have to be
  amended; 900 is a value we can grow into without a rules amendment.

**What 900 does NOT do:** it does not keep a checkpoint alive through a
full refresh failure, and no value can. Its job is to make that failure
visible and correctly labelled — `canonical_quote_stale`,
`is_valid_canonical_baseline = false`, and the coverage counts in the
evidence payload — instead of producing a confident-looking snapshot built
on the previous checkpoint's market.

---

## 4. Recommendation: failure policy — MODEL B

Chosen: **bounded explicit retries before the irreversible capture.**

The reasoning is the irreversibility. `capture_checkpoint` is idempotent
once CAPTURED, so "retry by calling the cycle again later" cannot work —
the first call has already consumed the checkpoint. A single transient
timeout would therefore permanently cost FINAL, which is the highest-value
checkpoint of the three. Retries must happen inside the same call, before
the capture, or not at all.

**Proposed budget: `max_attempts = 3`, `backoff_seconds = (30, 120)`.**

| input | reading |
| --- | --- |
| failure shape | Odds API failures we classify are transport-level (timeout, 5xx, rate limit) — transient on a seconds scale |
| window room | FINAL is 4 hours wide; a 150s retry budget is negligible against it |
| cost | worst case 3× the event-odds call, and only on failure |
| what 3 attempts cannot fix | a sustained outage — which is exactly when we should not be forecasting on last-known prices |

Two constraints beyond the brief:

**Not every failure is retryable.** `AUTHENTICATION_ERROR`,
`QUOTA_EXHAUSTED` and `QUOTA_RESERVE_EXHAUSTED` cannot succeed on a retry.
`MALFORMED_RESPONSE` is the sharpest case: the provider *answered* and we
were **billed**, so the same request yields the same unusable payload and
a retry pays again for it. Only `TIMEOUT`, `PROVIDER_UNAVAILABLE`,
`RATE_LIMITED` and `UNKNOWN_PROVIDER_ERROR` are retried.

**Retries must never consume the window they protect.**
`window_guard_seconds` (default 60) stops the loop when the next backoff
would leave too little of the window to capture in. Turning a recoverable
failure into a MISSED checkpoint would be a worse outcome than the failure.

Each attempt is a separate provider call with its own `ProviderCall` row
and appears individually in the cycle report. No adapter-level retry: that
would make three billed calls look like one, and "how many times did we
ask, and what did each attempt say" is research provenance.

---

## 5. Proposed frozen values

```python
max_observation_age_seconds = 900
refresh_retry_policy = {
    "max_attempts": 3,
    "backoff_seconds": [30, 120],
    "window_guard_seconds": 60,
}
```

Both are frozen because both materially change **which market state may
reach a checkpoint**. A capture is irreversible; letting an operator pick
either per run would mean two checkpoints in the same season were built
under different rules with nothing in the record saying so.

The schema is already in place (migration `f3a71d9b28c4`, both columns
nullable, with `max_observation_age_seconds >= 0` enforced by a DB CHECK).
`CapturePolicy.from_season_rules()` is the only official constructor and
carries `rules_version` as provenance, so a report can distinguish "the
season's rules said so" from "someone injected a value for this run".
Calibration and tests construct a policy directly and are marked
non-official by that same field.

**Remaining step on acceptance:** set the two values on the research
season's active `SeasonRules` row. That is one provisioning call, and a
later change is a rules amendment (a new row with `superseded_by`), never
an UPDATE.

---

## 6. Evidence

`app/tests/integration/test_checkpoint_cycle.py` — scenarios A–G:

| | scenario | result |
| --- | --- | --- |
| A | refresh succeeds immediately before capture | ages 0.0; no candidate distinguishable |
| B | refresh fails, observations 600s old | captured, baseline valid, labelled *(policy scenario, not a current path — §2)* |
| C | refresh fails, canonical 3600s old | baseline invalid, `canonical_quote_stale`, no substitution |
| D | one comparison book stale, canonical fresh | 1 excluded, context narrowed |
| E | no fresh comparison books | canonical-only: `books_observed` 4 vs `number_of_books` 1 |
| F | scheduler 3000s late, quotes fresh | large offset, ages 0.0 |
| G | scheduler on time, refresh failed earlier | offset 0.0, ages 5400 |

`app/tests/integration/test_checkpoint_preflight.py` — cost integrity and
the retry policy: zero provider calls for ALREADY_CAPTURED / TOO_EARLY /
EXPIRED / invalid policy, one for ELIGIBLE, bounded and individually
audited retries, non-retryable categories not retried, the window guard,
and at-most-once capture across retries.

Real-data anchor: the two live DET @ BUF runs, `04:19:20.012177Z` and
`20:10:56.225032Z`, 57096.2s apart.
