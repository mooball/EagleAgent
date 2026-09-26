# Plan: LLM Telemetry — First-Day Findings & Hardening

**Created:** 2026-09-25 · **A4 re-reviewed 2026-09-26: largely superseded by A11**
**Status:** 🔴 Findings captured — A1, A2 and A11 implemented 2026-09-26, full suite green
(1826 passed / 23 skipped). Not yet committed — this is the set intended to deploy.
A3, A5–A12 awaiting review.
**Branch:** `llm-benchmarks` (telemetry ships from here)
**Parent:** [plan-llmObservabilityAndFailover.prompt.md](plan-llmObservabilityAndFailover.prompt.md)
**Related:** [plan-agentTurnLatency.prompt.md](plan-agentTurnLatency.prompt.md) · [plan-pipelineReliability.prompt.md](plan-pipelineReliability.prompt.md) (stale, historical) · `scripts/prune_llm_call_log.py`

---

## Summary

P0.1 (telemetry) and P0.2 (failover repair) have now run in production for a
full working day. This document records **what the data actually says** and the
resulting action items, so they can be triaged in one sitting.

**Telemetry is working.** 712 rows collected from hour one across both call
paths. The failover repaired in P0.2 fired 160 times and rescued 159 of them —
that fix is paid for.

**But three things need attention:**

1. **`gemini-3.8-flash` is being rate-limited ~85–97% during peak hours** on
   `QUOTE/extract`. This is by far the biggest operational finding. The fallback
   we land on is 3× faster *and* cheaper, which reframes this as a
   model-selection problem rather than just a resilience one.
2. **The token columns are not usable for cost reporting as-is.** The two call
   paths mean different things by `output_tokens`, and there are silent NULLs
   that null out the arithmetic. Any cost report written on today's schema is
   wrong by ~70% on agent rows.
3. **The stated 45s retry budget is not enforced.** Observed calls run to 62.6s
   (success) and 91.4s (retry), because the budget is only checked *between*
   attempts.

Nothing in this document has been implemented. Each action item below is a
proposal with effort/risk, not a decision.

---

## Method & reproduction

All figures come from read-only `SELECT` queries against prod
(`PROD_DATABASE_URL`, Railway proxy). **No writes were made.**

The probes live in five throwaway scripts in the repo root —
`_diag_telemetry_prod.py` through `_diag_telemetry_prod5.py` — following the
existing `_diag_*.py` convention. They are untracked scratch files; see
**A7** for the proposal to fold the useful queries into a supported report.

To re-run the headline report:

```bash
uv run python _diag_telemetry_prod.py    # per-model health, errors, failover, nulls
uv run python _diag_telemetry_prod5.py   # failover pairing, retry gaps, terminal failure
uv run python scripts/prune_llm_call_log.py --stats   # the supported equivalent (thinner)
```

---

## The dataset

| | |
|---|---|
| Window | 2026-09-24 20:00 → 2026-09-25 08:10 UTC (09-25 06:00 → 18:10 AEST) |
| Rows | **712** |
| Token volume | **3,029,615** |
| Error rows | 162 (22.8%) — 161 are attempt-1 `429`, 1 is a terminal retry failure |
| Models seen | `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.1-pro-preview` |
| Scopes | `pipeline:QUOTE/extract`, `agent:ProcurementAgent`, `pipeline:QUOTE/classify`, `pipeline:QUOTE/interpret`, `pipeline:RFQ_CREATION/extract`, `agent:GeneralAgent` |

**Both instrumentation paths confirmed working in prod:**

| Path | Rows | Mechanism |
|---|---|---|
| `pipeline:*` | 585 | `llm_call_with_retry` + `instrument_call` (raw SDK) |
| `agent:*` | 127 | `LangChainTelemetryHandler` on `ChatGoogleGenerativeAI` |

This is the first evidence that the LangChain handler attachment works — there
was no way to test it locally.

**Writer health:** the newest LLM row is 08:10 UTC, which looked alarming until
cross-checked — `email_tracking` was written at 09:20 and `supplier_brands` at
10:49, so the app was alive and simply had no LLM work during an idle Friday
evening. Telemetry itself has no gaps attributable to failure (largest LLM
silence, 40 min, is consistent with idle periods).

---

## Findings

### F1 — `gemini-3.8-flash` 429s escalate to near-total failure in peak hours

**161 of 298 `gemini-3.8-flash` calls failed (54%) — every single one a 429.**
It is not spread across the day; it ramps up and stays up:

| Hour (UTC) | calls | 429s | % |
|---|---|---|---|
| 22:00 (09-24) | 17 | 1 | 5.9% |
| 23:00 (09-24) | 20 | 2 | 10.0% |
| 00:00 | 42 | 2 | 4.8% |
| 01:00 | 16 | 13 | **81.3%** |
| 02:00 | 59 | 57 | **96.6%** |
| 03:00 | 33 | 26 | **78.8%** |
| 04:00 | 51 | 45 | **88.2%** |
| 05:00 | 18 | 15 | **83.3%** |

**Contained to two scopes**, and to this model only:

| scope | model | calls | errors |
|---|---|---|---|
| `pipeline:QUOTE/extract` | `gemini-3.8-flash` | 240 | 130 |
| `pipeline:QUOTE/interpret` | `gemini-3.8-flash` | 58 | 31 |
| *everything else* | `3.5-flash-lite` / `3.6-flash` / `3.1-pro-preview` | 414 | **1** |

The other models sit at 0–0.4%. So this is not a project-wide quota ceiling or
a global outage — it is quota, or a per-model limit, on that one model.

```sql
-- the escalation table
SELECT to_char(date_trunc('hour', ts), 'MM-DD HH24:00') AS hour_utc, model,
       count(*) AS calls,
       count(*) FILTER (WHERE http_status = 429) AS n429,
       round(100.0*count(*) FILTER (WHERE http_status = 429)/count(*),1) AS pct429
FROM llm_call_log
WHERE split_part(scope, ':', 1) = 'pipeline' OR http_status = 429
GROUP BY 1,2 HAVING count(*) FILTER (WHERE http_status = 429) > 0
ORDER BY 1,2;
```

---

### F2 — The fallback is strictly better than the primary

This is the finding that reframes F1. The model we fail *over to* beats the one
we fail *from* on both speed and cost:

| | `gemini-3.8-flash` (primary) | `gemini-3.5-flash-lite` (fallback) |
|---|---|---|
| Successful calls | 137 | 264 |
| Error rate | 54.0% | **0.4%** (1 call) |
| avg latency | 5,872 ms | **2,383 ms** |
| p90 latency | 14,370 ms | **3,406 ms** |
| p95 latency | 26,702 ms | **4,841 ms** |
| max latency | 62,648 ms | 23,925 ms |
| avg tokens/call | 1,779 | 1,441 |

Almost every "successful" 3.8-flash call is also slow and expensive by
comparison. On this day's evidence, `gemini-3.8-flash` looks like the wrong
primary for `QUOTE/extract` — but see **A4**, because throughput and quality are
not the same thing and we have no quality signal yet.

---

### F3 — Failover works, but one call failed outright

- 161 attempt-1 429s → **160 retries logged** → 159 succeeded, 1 failed.
- The terminal failure: **2026-09-25 04:13 `pipeline:QUOTE/interpret`** — the
  `flash-lite` retry *also* returned 429, after **91.4 seconds**. This is the
  only end-to-end failure in the window.
- Retry cost is modest: 144 of 161 retries landed 2–5s after the error
  (the rest are longer `Retry-After` waits). Total time added by the whole
  429 storm is roughly 13 minutes of wall clock, not hours.

**Unresolved accounting discrepancy:** 161 attempt-1 errors but only 160
attempt-2 rows. Chronological pairing (next attempt-2 in the same scope) claims
all 161 were retried, which is only possible if one chain's retry row is absent
from the table. Since no user-visible failure corresponds to it, the leading
hypothesis is a **silent telemetry drop** — telemetry uses a bounded queue and
drops are only counted in an in-process counter that nothing exposes. It is not
attributable read-only. See **A10**.

---

### F4 — `LLM_MAX_ATTEMPT_SECONDS = 45` is not enforced

The budget is checked at the **top of each attempt only**, never mid-flight. A
single in-progress call therefore runs until the HTTP timeout
(`LLM_REQUEST_TIMEOUT_MS = 120000`) regardless of the budget.

Evidence of the 45s ceiling being exceeded:

| timestamp | scope | model | latency | status |
|---|---|---|---|---|
| 09-24 21:40 | `pipeline:QUOTE/extract` | `gemini-3.8-flash` | **62,648 ms** | ok |
| 09-25 04:13 | `pipeline:QUOTE/interpret` | `gemini-3.5-flash-lite` | **91,419 ms** | error (429) |

Three successful calls exceed 45s, none exceed 120s — consistent with 120s being
the real ceiling. `includes/email_pipeline.py` documents the budget as
"give up at `LLM_MAX_ATTEMPT_SECONDS`", which does not match observed behaviour.

---

### F5 — The two call paths disagree about `thought_tokens`

Committed as a comment in the migration ("stored separately because thinking
bills at the output rate") — but the column means different things depending on
which path wrote the row:

| path | identity that holds | meaning of `output_tokens` |
|---|---|---|
| `pipeline:*` (raw SDK wrapper) | `total = prompt + output + thought` | **excludes** thoughts |
| `agent:*` (LangChain handler) | `total = prompt + output` | **already includes** thoughts |

So on agent rows, `output_tokens + thought_tokens` **double-counts**:

| path | calls | thoughts | output | thoughts as % of total |
|---|---|---|---|---|
| `pipeline` | 423 | 94,715 | 97,608 | 9.5% |
| `agent` | 127 | 49,070 | 67,171 | 2.4% |

On agent rows, 49,070 thoughts against 67,171 output is a **+73% inflation** if
you add them. Verified by SQL: every one of the 127 agent rows satisfies
`total = prompt + output` and none satisfies `total = prompt + output + thought`.

**Until this is normalised, derive cost from `total_tokens` only.**

---

### F6 — Silent NULLs break token arithmetic

- **`thought_tokens` is NULL on all 264 `gemini-3.5-flash-lite` rows** — the
  model never reports `thoughts_token_count`. It is also NULL on 6 of 137
  successful `gemini-3.8-flash` rows.
- **One row has `output_tokens = NULL` while `total_tokens` is populated**
  (2026-09-24 21:09:48, `QUOTE/extract`): `prompt=1099, output=NULL,
  thought=109, total=1208`. Because SQL arithmetic with NULL yields NULL, this
  row silently matches *neither* token identity and disappears from any
  arithmetic-based reconciliation.

Rows with NULL `prompt_tokens` (22.8%) are the error rows, which is expected —
a failed call has no usage. That part is fine.

---

### F7 — Four columns are effectively dead

| column | state | note |
|---|---|---|
| `correlation_id` | **NULL on all 712 rows** | Plumbed through `email_pipeline.py`, but no caller supplies `thread_id` / `rfq_number`. The column's stated purpose — "which RFQ was this call for" — is currently unanswerable. |
| `ttft_ms` | **NULL on all 712 rows** | `set_ttft_ms()` has no callers. Nothing streams, so there is nothing to measure. |
| `service_tier` | **NULL on all 712 rows** | Production requests no tier at all. Either `INTERACTIVE_SERVICE_TIER` / `SYNC_SERVICE_TIER` are unset in Railway, or they never reach the call sites. The P0.2 tier plumbing is wired but unexercised. |
| `location` | `global` on all rows | Expected (Gemini 3.x is global-only), and Vertex won't report the serving region anyway. |

**Also:** the `sync:` scope prefix is **never emitted** — the census is 585
`pipeline:` / 127 `agent:`. P0.3's workload-signal plumbing does not fire, which
is consistent with P0.3 being blocked on the "how do we tell a sync trigger from
a user trigger" decision recorded in the parent plan.

---

### F8 — Agent token consumption dominates, and one turn reached 41k prompt tokens

| scope | model | calls | prompt | output | thoughts | total | avg/call |
|---|---|---|---|---|---|---|---|
| `agent:ProcurementAgent` | `gemini-3.6-flash` | 125 | 1,962,455 | 65,940 | 48,042 | **2,028,395** | 16,227 |
| `pipeline:QUOTE/extract` | `gemini-3.8-flash` | 110 | 131,826 | 36,571 | 27,342 | 195,739 | 1,779 |
| `pipeline:QUOTE/extract` | `gemini-3.5-flash-lite` | 129 | 160,597 | 25,323 | 0 | 185,920 | 1,441 |
| `pipeline:QUOTE/interpret` | `gemini-3.8-flash` | 27 | 136,060 | 9,454 | 40,036 | 185,550 | 6,872 |
| `pipeline:QUOTE/interpret` | `gemini-3.5-flash-lite` | 30 | 141,855 | 13,853 | 0 | 155,708 | 5,190 |
| `pipeline:QUOTE/classify` | `gemini-3.5-flash-lite` | 105 | 140,899 | 6,757 | 0 | 147,656 | 1,406 |
| `pipeline:RFQ_CREATION/extract` | `gemini-3.1-pro-preview` | 22 | 91,646 | 5,650 | 27,337 | 124,633 | 5,665 |
| `agent:GeneralAgent` | `gemini-3.6-flash` | 2 | 4,783 | 1,231 | 1,028 | 6,014 | 3,007 |

**`agent:ProcurementAgent` alone is 67% of all tokens**, driven by prompt size
(avg ~15.7k prompt tokens/call). At 04:01 a single turn made five calls of
~38–41k prompt tokens each — ~200k tokens for one interaction, and the largest
single prompt in the window was **41,025 tokens**. Related to
`plan-agentTurnLatency.prompt.md` (15.8k-char system prompt); that plan already
proposes trimming it, and this data supports doing so on cost grounds too.

---

## Action items

Ordered by my recommendation. **None of these are approved yet** — this section
is the review surface.

### A1 — Enforce the retry budget (F4) · P0 · ✅ **implemented 2026-09-26**

Folded together with **A11** (see below). Implemented as option (a):

- Each attempt's HTTP timeout is now
  `min(remaining_budget, LLM_REQUEST_TIMEOUT_MS)`, with a 1s floor so a tiny or
  misconfigured budget still makes one real attempt instead of failing with a
  misleading "no model candidates available".
- The between-attempts deadline check is retained, so a call that has run out of
  budget stops rather than starting another attempt with a nonsense timeout.

**Judgement call needing review:** the default budget moved **45s → 90s**. A
strict 45s budget would have been a regression, not a fix — the slowest
*successful* call in prod telemetry is 62.6s (`gemini-3.8-flash`,
`pipeline:QUOTE/extract`, 2026-09-24) and `RFQ_CREATION/extract` runs p99 ≈ 44.7s,
so enforcing 45s would have converted those successes into failures. 90s still
improves the real worst case enormously: before enforcement it was
`n_candidates × LLM_REQUEST_TIMEOUT_MS` = 240s for two candidates, and with the
longer ladder (A11) it would have been 480s.

If 90s is not the number you want, it is now a single honest config value rather
than a fiction, which was the actual point of the fix.

---

### A2 — Normalise token capture (F5, F6) · P0 · ✅ **implemented 2026-09-26**

`total_tokens == prompt_tokens + output_tokens + thought_tokens` now holds for
**both** call paths. Prerequisite for any cost report or cost-based decision.

1. **`_tokens_from_usage()`** (new, in `includes/llm/telemetry.py`) maps a
   LangChain `usage_metadata` dict onto our columns and **subtracts reasoning
   from `output_tokens`**. LangChain's own identity is `total = input + output`
   with reasoning *inside* output, so removing it is what makes the two paths
   agree.
   - Side benefit: the `llm_output['usage_metadata']` branch previously never
     captured `thought_tokens` at all — a latent second version of this bug.
     Both branches now go through the one helper.
2. **`_apply_token_invariant()`** (new) fills a component the provider omitted
   whenever `total_tokens` is present. One missing value was enough to NULL out
   every derivation — that is exactly how the prod row was found. With no total
   it does nothing, because there is no invariant and inventing zeros would be a
   lie.
3. **Regression test added**: `test_raw_sdk_and_langchain_agree_on_the_same_call`
   feeds the same logical call to both adapters and asserts the resulting token
   columns are **identical**, plus 4 more tests covering the subtraction, the
   missing-component fill, the no-total case, and row shape.
4. **`_normalise()` now emits all 18 columns** rather than only the keys the
   caller passed, so a derived value cannot give one row a different shape from
   its batch-mates.
5. **Invariant documented** in the module docstring. No DB-level comment — that
   would need a migration, and this change is migration-free so it can ship
   without touching the schema.

**Verified:** full suite **1826 passed / 23 skipped / 0 failed**. Real insert
path exercised against the local table (rolled back; stray rows cleaned up).

**Still open — historical rows.** Every pre-2026-09-26 `agent:*` row still has
reasoning counted inside `output_tokens`, so `output + thought` still
 double-counts for those. Options are to document it (analysis must special-case
`ts < 2026-09-26` for `agent:` scopes) or backfill:

```sql
-- PROD WRITE — needs explicit approval. Agents only; the pipeline path was
-- already correct.
UPDATE llm_call_log
   SET output_tokens = output_tokens - thought_tokens
 WHERE scope LIKE 'agent:%'
   AND thought_tokens IS NOT NULL AND output_tokens IS NOT NULL
   AND ts < '2026-09-26';
```

At ~127 historical agent rows the backfill is cheap, but it rewrites evidence,
so it is your call.

---

### A3 — Record which model actually served each pipeline result (F1, F2) · P0

Today a `QUOTE/extract` result carries no marker, so ~54% of extraction output
is silently produced by a different model than the one configured. That makes
quality comparisons across emails meaningless and would make any 3.8-vs-3.5
decision untrustworthy.

Persist the serving model (and whether it was an attempt>1 fallback) alongside
the pipeline result. Requires finding the storage point in
`includes/tools/supplier_quote_pipeline.py` (`supplier_pipeline_result` is
already JSON on `email_tracking`, so this may be additive).

**Effort:** ~1–2h. **Risk:** low, but touches stored payload shape — check what
reads `supplier_pipeline_result` before changing it.

---

### A4 — `QUOTE` model strategy · ⚪ **largely superseded by A11 (reviewed 2026-09-26)**

Re-reviewed after the ladder shipped. The original framing assumed a *flat*
fallback chain, which meant every 429 sent ~54% of `QUOTE/extract` traffic to
`flash-lite` — a different class of model. That is no longer what happens: the
first fallback is now `3.6-flash`, the same generation family and price tier as
`3.8-flash`.

**Now irrelevant:**

- **Option 2** ("give `QUOTE/extract` its own candidate list that does not lead
  with `3.8-flash`") — this is exactly what A11 implements, generically, for
  every pipeline rather than bespoke to one.
- **Option 4** ("do nothing yet") — was the de facto outcome, and remains
  defensible.
- **The quality-cliff framing** — `3.8` → `3.6` is an incremental capability step,
  not the 3.8 → flash-lite drop the finding was written about.

**Now actively wrong:**

- **Option 1** ("make `flash-lite` primary for `QUOTE/extract`") **would delete
  its failover entirely.** `flash-lite` is the bottom rung, so
  `ladder[index(lite):]` is a one-element list. Under the old flat chain it at
  least had a distinct fallback; under the ladder it would have none. The
  option that looked best on measured latency and cost is now the worst option
  on resilience. **Rule this out.**

**Still live, but relocated:**

- **Option 3 (fix the quota)** is unchanged and real — the 429s are a capacity
  problem that the ladder routes *around* rather than fixes. Folded into **A6**
  (service tier), noting A11 makes this less urgent because degradation is now
  graceful.
- **The quality question** is now a narrower **3.8 vs 3.6** question, and it
  remains unanswerable from telemetry. It is gated on **A3** — and A3 has become
  *more* important, not less, because the ladder deliberately produces a model mix
  that is currently invisible in the stored result.

**New residual risk introduced by A11** → see **A12**.

**Effort:** none outstanding. **Risk:** n/a — no further change recommended.

---

### A5 — Plumb `correlation_id` (F7) · P1

Supply `thread_id` / `rfq_number` at the pipeline call sites so "which RFQ was
this call for?" becomes answerable. Without it, per-RFQ cost, per-RFQ retry
count, and "did this RFQ get degraded output" are all impossible.

Note the parent plan's P0.3 blocker is the same shape of problem: the trigger
context is not threaded through. Fixing both at once may be cheaper than twice.

**Effort:** ~2h (audit ~6 call sites). **Risk:** low — additive.

---

### A6 — Decide the service-tier question (F7) · P1

`service_tier` is NULL on every row, so we are on Standard everywhere and the
Priority/Flex plumbing is dead weight. Either:

- **set** `INTERACTIVE_SERVICE_TIER` / `SYNC_SERVICE_TIER` in Railway and verify
  rows start carrying a value (cheap, and Priority's graceful downgrade is the
  real 429 mitigation from the parent plan); or
- **remove** the plumbing, and revisit with the registry work.

Leaving it wired-but-unset means the parent plan's headline mitigation is
unexercised and untested.

Caveat carried from the parent plan: Vertex does not report the tier that
*actually served* a call, so once set, we still cannot detect a silent
Priority→Standard downgrade.

**Effort:** ~30 min to set + verify. **Risk:** low, but it changes cost/latency
characteristics — measure with telemetry either side.

---

### A7 — Turn these probes into a supported report (F1–F8) · P1

The five `_diag_telemetry_prod*.py` scripts are throwaway. `prune_llm_call_log.py
--stats` exists but is thin (auto-vacuum lag not withstanding, it shows totals
and a 24h per-model table only).

Extend `show_stats` — or add a sibling `scripts/llm_telemetry_report.py` — with
the queries that actually earned their place: 429-rate by hour, failover
pairing, null-rate audit, token-identity check, scope×model breakdown. That
makes this a recurring 30-second check instead of a re-derivation each time.

Then delete the `_diag_*` scratch files.

**Effort:** ~2h. **Risk:** none — read-only tooling.

Related, and probably the better long-term home: the parent plan's **P4 Admin UI**
already anticipates a model-health panel, and
`includes/dashboard/routes/admin.py` already exists with `require_admin`. A
"LLM health" section there would make this visible without SSH or scripts.

---

### A8 — Make silent telemetry drops visible (F3) · P1

We cannot currently distinguish "no LLM calls happened" from "telemetry dropped
rows", and F3's unexplained 161-vs-160 discrepancy is likely exactly this.

Expose the in-process `written` / `dropped` / `failed` counters — the parent
plan assumed they were log-only, but a small read-only field on an existing
health/status route (or an admin fragment per A7) would close the loop. Log a
WARNING on first drop so it appears in Railway even without a route.

**Effort:** ~1h. **Risk:** none.

---

### A9 — Unblock the sync/workload signal (F7) · P2

`current_workload()` never returns `SYNC` in production, so P0.3 cannot be
resolved and sync-vs-user traffic stays indistinguishable — which also means we
cannot yet answer "is the background sync causing the 3.8-flash 429s?" (F1 is
suspiciously concentrated in exactly the pipeline the sync loops drive).

This is the parent plan's known blocker. Options: a contextvar set by the
`main.py` loops, or explicit per-pipeline config. **Needs the same decision the
parent plan deferred — do not guess.**

**Effort:** unknown (design decision first). **Risk:** medium — must not
mislabel user-triggered traffic.

---

### A10 — `ttft_ms`: wire or remove (F7) · P2

NULL on all 712 rows with no callers. Either implement streaming and wire
`set_ttft_ms()`, or drop the column in a migration. Carrying a dead column in
the schema invites someone to report on it.

**Effort:** ~15 min to drop. **Risk:** none.

---

### A11 — Relative model ladder (F1, F2) · ✅ **implemented 2026-09-26**

Raised by the reviewer after reading F1/F2: *"all models fall over to the same
backup, which means high thinking models fall back to very low ones."* Correct,
and it was worse than it looked.

**The old behaviour.** `get_pipeline_candidates()` returned
`[primary] + FALLBACK_CHAIN` where the chain was flat
(`flash-lite, 3.6-flash`). Three consequences:

1. Every primary fell to `flash-lite` — including
   `RFQ_CREATION_EXTRACT_MODEL=gemini-3.1-pro-preview`, a **Pro model dropping
   straight to lite**, which is the single biggest capacity cliff in the system.
2. The second entry (`3.6-flash`) was **dead code**: candidate #2 always
   succeeded, so it was never reached. Prod confirms it — all 127 `3.6-flash`
   rows are `agent:` scope with `attempt = NULL`, i.e. it has never once been
   used as a pipeline fallback.
3. `QUOTE_CLASSIFY_MODEL=gemini-3.5-flash-lite` fell *upward* to `3.6-flash` —
   a slower, more expensive model as the "safety net".

**The new behaviour.** `Config.MODEL_LADDER` is one ordered list, most capable
first, and a model's fallbacks are the entries **below it**:

```
gemini-3.1-pro-preview > gemini-3.8-flash > gemini-3.6-flash > gemini-3.5-flash-lite
```

| primary | fallbacks now | before |
|---|---|---|
| `gemini-3.1-pro-preview` | 3.8 → 3.6 → lite | lite |
| `gemini-3.8-flash` | 3.6 → lite | lite |
| `gemini-3.6-flash` | lite | lite |
| `gemini-3.5-flash-lite` | *(none)* | 3.6-flash (upward) |
| anything unranked | lite only | lite |

Two previous bug classes are now impossible by construction: a fallback can never
be the primary (the primary is always index 0 of its own list), and a fallback can
never be more expensive than the primary (unranked primaries get the cheapest rung
only, with a warning logged).

**`3.7-flash` is deliberately excluded**, against the original proposal
(`3.8 > 3.7 > 3.6 > lite`). Our own benchmark has it at **7.01s mean wall vs 3.10s
for 3.6-flash** — a 2.3× regression — with no measured quality gain (all five
models scored 10/10 on the one hand-labelled task). Because 3.6/3.7/3.8 share
identical per-token pricing, the ladder is a *capability* ladder, not a price one.
Add 3.7 back only if a quality eval earns it a place.

**Why this should actually work** (rather than just add latency): the 429s are
**per-model, not a shared quota**. In the 2026-09-25 01:00–05:30 storm,
`3.8-flash` was 156/175 (89%) throttled while `3.6-flash` served 55/55 and
`3.1-pro-preview` 12/12 unthrottled in the *same window*. Verified standalone:
`_verify_chain.py`.

**Accepted trade-off:** the bottom rung has no fallback, so a 429 on
`flash-lite` is terminal. Decision taken deliberately — there is nothing cheaper
to step down to, and retrying the same overloaded model is the anti-pattern this
function exists to avoid. Service-tier Priority (**A6**) is the intended
mitigation.

**Env migration:** `FALLBACK_MODEL` / `FALLBACK_CHAIN` are now deprecated no-ops
that log a warning at import if set. Worth checking whether Railway still sets
them — I cannot read prod env vars from here.

---

### A12 — Measure whether the ladder's landing spot holds up (A11 residual) · P1

A11 changes *where* the load goes, and the new landing spot is an extrapolation
rather than a measurement. The first fallback for `QUOTE/extract` is now
`3.6-flash`, which has **never been driven at pipeline volumes**:

| | observed | after A11 (worst hour) |
|---|---|---|
| `3.6-flash` busiest hour | 35 calls | **~69 calls** (12 agent + 57 redirected) |
| `flash-lite` busiest hour | 71 calls, 1 × 429 | — |

The optimistic read is that `flash-lite` provably handles 71/hour with a single
429, so `3.6-flash` at ~69/hour should be fine. But that is a different model
with unknown per-model headroom, and it must also keep serving the agents. If it
does strain, the ladder will simply hop through `3.6-flash` to `flash-lite` —
trading back some of the quality gain for latency, and making A6 (Priority tier)
more pressing.

**This is now measurable**, which it was not before — query the 429 rate for
`gemini-3.6-flash` on Monday afternoon and compare against Monday's
`gemini-3.8-flash` rate:

```sql
SELECT date_trunc('hour', ts) AS hour_utc, model,
       count(*) AS calls, count(*) FILTER (WHERE http_status = 429) AS n429
FROM llm_call_log
WHERE ts > now() - interval '1 day'
GROUP BY 1,2 ORDER BY 1,2;
```

**Cost note (already true, worth recording):** for the `3.8` → `3.6` hop the
ladder steps *up* in cost, not down. `3.8-flash` is unusually cheap
(~$1.43/1k calls) because it uses ~4× fewer thinking tokens than `3.6-flash`
(~$2.28/1k). So this is a **capability** ladder, with a mildly non-monotonic
price. Absolute impact is ~**28¢ per day** for the 161 redirected calls —
negligible, but the "incremental change in capacity *or price*" goal holds for
capability only.

**Effort:** ~10 min to check. **Risk:** none.

---

## Decisions needed from the reviewer

| # | Question | Blocks |
|---|---|---|
| 1 | ~~A1: enforce the 45s budget, or re-document it?~~ **Resolved** — enforced; default raised to 90s (see A1) | — |
| 2 | ~~A4: change `QUOTE/extract`'s primary model?~~ **Resolved 2026-09-26** — superseded by A11; option 1 would now remove failover entirely | — |
| 3 | A6: set the service tiers in Railway, or remove the plumbing? | A6 |
| 4 | A2 residual: backfill the 127 historical `agent:` rows, or document that `output + thought` double-counts before 2026-09-26? **Needs approval — it is a prod UPDATE.** | A2 |
| 4 | A9: contextvar vs per-pipeline config for the sync signal? | A9, P0.3 |
| 5 | A7: script, or admin UI section (parent plan P4)? | A7 |
| 6 | A8: acceptable to surface telemetry drop counters on an existing health route? | A8 |
| 7 | Retention: is 30 days still right now that volume is ~700 rows/day (≈21k/month)? | pruning |

---

## Suggested sequencing

Given the weekend window and that F1 recurs Monday morning:

**Done 2026-09-26:** A1 + A11 (folded together) — the model ladder and the
enforced attempt budget.

**Do next (low risk, high value, no decisions needed):**
A3 · A10 · A8

**Do after a decision:**
A6 (30 min) · A7 · A5

**Wait for evidence / a human:**
A9 (design question)

**Time-triggered:** A12 — check `3.6-flash`'s 429 rate on Monday afternoon, once
A11 has had a working day of real pipeline traffic behind it.

**Blocked on A3 first:** the 3.8-vs-3.6 quality question (A4's only surviving part).

**Explicitly not in scope here:** the model registry, automatic capability
routing, the benchmark harness, and cost *reporting* — all remain in the parent
plan. This document is deliberately limited to making the data we already have
trustworthy and acting on what it plainly says.

---

## Verification

- `uv run pytest tests/ --timeout=60 -q --no-header --ignore=tests/agents/test_browser_agent.py`
  — **1821 passed, 23 skipped, 0 failed** (2026-09-26, 106s). The long-standing
  `tests/tools/test_rfq_crud.py` dev-DB flakiness did not appear on this run.
- Standalone check that needs no database: `uv run python _verify_chain.py`
  — asserts candidate order per primary, the unranked and empty-ladder edges, the
  budget-bounded per-attempt timeout, and the 3.8 → 3.6 → lite attempt sequence.
  Passed 2026-09-26.
- New tests added for A1/A11 in `tests/test_email_pipeline.py`:
  `test_candidates_are_the_ladder_below_the_primary`,
  `test_bottom_of_ladder_has_no_fallback`,
  `test_unranked_primary_falls_back_only_to_the_cheapest_rung`,
  `test_attempt_timeout_is_bounded_by_remaining_budget`,
  `test_gives_up_when_budget_is_exhausted`.
- New test: both telemetry adapters yield the **same** token identity for the
  same synthetic usage payload (guards F5 regressing).
- New test: a call whose latency exceeds the attempt budget is aborted at the
  budget, not at the HTTP timeout (guards A1).
- New test: a fallback-marked result is persisted with its serving model (A3).
- Manual, post-deploy:
  ```sql
  -- identity must hold for every row with tokens
  SELECT split_part(scope, ':', 1) AS path, count(*) AS calls,
         count(*) FILTER (WHERE total_tokens = prompt_tokens + output_tokens + thought_tokens) AS ok_identity
  FROM llm_call_log WHERE total_tokens IS NOT NULL GROUP BY 1;

  -- null audit: tier and correlation should start populating
  SELECT round(100.0*count(*) FILTER (WHERE service_tier IS NULL)/count(*),1) AS tier_null_pct,
         round(100.0*count(*) FILTER (WHERE correlation_id IS NULL)/count(*),1) AS corr_null_pct
  FROM llm_call_log WHERE ts > now() - interval '6 hours';
  ```
- Operational: re-run the F1 hourly 429 table Monday afternoon and compare
  against the 81–97% baseline recorded here.

---

## Notes for the reviewer

- The 429 storm in F1 happened **before** this document existed and is still
  unmitigated. A4 is the fix and A4 is blocked on a quality judgement, so
  Monday morning will look like Friday did. That is the strongest argument for
  picking **A4 option 4 (do nothing)** deliberately rather than by default —
  the failover is absorbing it, at the cost of a silent model mix.
- F2 is counter-intuitive enough to deserve scepticism: a *lite* model beating a
  full model on latency is expected, but the near-zero error rate is the
  surprising part and might itself be an artefact of lower request volume. Worth
  re-checking next week before acting on it structurally.
- F5 is the finding most likely to cause a wrong decision later, because it is
  invisible unless you write the reconciliation query. It should be fixed before
  anyone builds cost reporting on top.
