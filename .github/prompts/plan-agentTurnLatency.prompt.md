# Plan: Agent Turn Latency — why "add supplier to line N" takes 60s

**Created:** 2026-09-23
**Status:** 🔴 Diagnosis complete — awaiting fix decision
**Baseline branch for prod behaviour:** `main` (deployed)
**Evidence source:** production Railway logs, 2026-09-23 05:34:44→05:35:46 UTC

---

## Summary

A one-line RFQ action ("please add sydney tools to line 1", RFQ-2026-1054) took
**60.8s**. The user's expectation is a few seconds.

**~58 of the 61 seconds was spent waiting on Vertex AI — not doing work.** The
actual business logic (supplier DB lookup + RFQ write) took **~0.35s**.

The dominant cause is **HTTP 429 `RESOURCE_EXHAUSTED` from Vertex AI plus silent
exponential-backoff retries inside `langchain_google_genai`**. The user sees an
endless "Agent working…" spinner because the SDK retries below our logging and
never surfaces an error.

This plan covers: (a) reducing what we ask of the model, (b) reducing how many
times we ask, (c) surfacing throttling instead of hiding it, and (d) adding a
deterministic fast path for the most common staff action.

---

## Evidence

### 1. Where the 61s actually went

From the runner's existing `[TIMING]` instrumentation
(`includes/chat/runner.py`, gaps > 500ms):

| Phase | Time | Notes |
|---|---|---|
| Supervisor intent classification (LLM) | 2.3s | `gemini-3.5-flash-lite` |
| Agent LLM call #1 — choose `search_suppliers` | **16.7s** | no 429 logged; raw slowness |
| `search_suppliers` tool | 0.14s | DB only (stage-1 hit) |
| Agent LLM call #2 — **3 × 429 retries** | **37.5s** | see log below |
| `manage_rfq add_supplier` (the DB write) | ~0.2s | |
| Agent LLM call #3 — write the prose answer | 4.0s | |
| **Total** | **60.8s** | |

```
INFO google_genai._api_client: Retrying ... in 1.42 seconds as it raised
  ClientError: 429 Too Many Requests. "Resource exhausted. Please try again later."
INFO google_genai._api_client: Retrying ... in 2.12 seconds ...
INFO google_genai._api_client: Retrying ... in 4.76 seconds ...
```

### 2. The project is being throttled at ~2 requests/minute

Over a 73-minute production log window:

- **115** `generateContent` calls → **1.6/min**
- **22** × `429 RESOURCE_EXHAUSTED`
- Breakdown by model: `gemini-3.8-flash` 83, `gemini-3.5-flash-lite` 25,
  `gemini-3.1-pro-preview` 7

The 429s hit the interactive chat, RFQ validation, web-search research, and the
email pipeline alike — roughly one every 3 minutes, project-wide.

Note the error wording: **"Resource exhausted. Please try again later."** — the
*capacity* form, not the named-quota form (`"Quota exceeded for quota metric
'Generate content request count'…"`). Every model configured is a **preview**
model (`gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-pro-preview`,
`gemini-embedding-2-preview`) — preview models have limited shared capacity and
no SLA.

### 3. Production runs a different, heavier model than dev

| Env | `DEFAULT_MODEL` | ProcurementAgent resolves to |
|---|---|---|
| Production (Railway) | `gemini-3.8-flash` | `gemini-3.8-flash` (`PROCUREMENT_AGENT_MODEL` unset) |
| Local `.env` | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` |

A/B of the identical 10,428-token payload, same Vertex project, from Sydney:

| Model | run 1 | run 2 | run 3 |
|---|---|---|---|
| `gemini-3.8-flash` | 4.08s | 3.48s | 4.44s |
| `gemini-3.5-flash-lite` | 2.04s | 1.37s | 1.13s |

**Neither is 16.7s.** So the 16.7s first call in production is *not* raw model
latency or network — it is contention. Scope this properly in Phase 1.

### 4. Per-call payload is large

| Component | Size |
|---|---|
| Tool schemas (22 tools, serialised) | 58,505 chars ≈ 14,600 tokens |
| — of which `manage_rfq` | 23,204 chars (**40% of all tool schema**) |
| — of which `department_prompt_table()` appended to `manage_rfq` | 1,578 chars |
| System prompt `config/prompts/procurement_agent.md` | 15,796 chars ≈ 3,950 tokens |
| **Measured input tokens per call** | **10,428** |

`manage_rfq`'s action catalogue docstring (`includes/tools/quote_tools.py`,
~21,600 chars **excluding** the department table) is the single biggest item.
No prompt/context caching is in use.

### 5. Three sequential LLM round-trips for a deterministic action

1. `Supervisor` → `classify_intent` (`includes/agents/supervisor.py:106-165`) —
   called for **every** user message; there is no rule-based fast path for user
   text (the string-matching branches only handle AI messages / tool prefixes).
2. `ProcurementAgent` call #1 → picks a tool.
3. `ProcurementAgent` call #3 → writes the prose summary.

Total: **~3 model calls + 2.3s routing for one DB write.** Under 429 pressure,
each of those three is independently exposed to a retry storm.

---

## Root causes

1. **Vertex AI throttling + silent SDK backoff.** `langchain_google_genai`
   retries 429s internally at INFO level with exponential backoff. Our own
   `_notify_retry` (`includes/agents/base.py`) never fires because the SDK
   swallows the error first, and if it does exhaust, app-level
   `MAX_RETRIES=3 / RETRY_BASE_DELAY=10` adds 10s then 20s **on top**.
2. **Background LLM traffic shares the interactive model.**
   `main.py` runs Gmail and NetSuite sync loops every `*_SYNC_INTERVAL=300s`
   in-process, and `email_pipeline.get_pipeline_model()`
   (`includes/email_pipeline.py:56`) resolves to `Config.DEFAULT_MODEL` — the
   *same* `gemini-3.8-flash` as the chat agent. Worse,
   `llm_call_with_retry` treats 429 as transient and immediately re-hits the
   *same* overloaded model twice (1s/2s backoff) before falling back, so
   background jobs amplify the storm instead of backing off.
3. **Preview models everywhere** → limited shared capacity.
4. **Heavy per-call payload** (~10.4k tokens, driven by the tool catalogue).
   Every wasted token is extra capacity pressure and extra latency.
5. **Round-trip count** — 3 model calls for one deterministic operation.

---

## Open questions (need a decision before Phase 1)

- **Q1. Which GA model replaces the preview models for interactive use?**
  Needs a model with guaranteed capacity + a separate quota bucket from the
  email pipeline. Candidates to benchmark: a GA flash-tier model in
  `australia-southeast1`.
- **Q2. Region.** Stay on `locations/global`, or pin to
  `australia-southeast1` for a separate quota bucket and lower latency from
  Sydney? (Global also risks cross-region routing.)
- **Q3. Model separation.** Should the email pipeline and the interactive agent
  deliberately use *different* models so background work can never starve chat?
  I think yes — cheap and independent.
- **Q4. Do we want the deterministic "add supplier" fast path at all**, or is
  fixing the model/quota enough? The fast path is ~300× faster for the common
  case but adds a new UI surface.
- **Q5. Branch.** Prod is `main`; the current working branch `rfq-refinements`
  has an unapplied migration (`c7d8e9f0a1b2_add_pipeline_activity_to_rfqs`) not
  present in prod. This work should branch from `main`.

---

## Phases

### Phase 1 — Stop the bleeding: model + quota (config-first, no code)

Goal: kill the 37.5s retry storm without touching business logic.

1. Set `PROCUREMENT_AGENT_MODEL` / `GENERAL_AGENT_MODEL` explicitly
   (currently unset → falls through to `DEFAULT_MODEL`). Re-read
   `config/settings.py:65-82, 243-254`.
2. Move interactive chat onto a **GA** model with real capacity.
3. Give the email pipeline its own model so background sync can't contend
   (Q3).
4. Evaluate `GOOGLE_CLOUD_LOCATION` (Q2).
5. If throttling persists on a GA model, request a Vertex AI quota increase.

**Acceptance:** repeat the "add sydney tools to line 1" turn 10× in production;
median < 8s, p95 < 15s, and **zero** `429` retry lines in the logs.

---

### Phase 2 — Surface throttling instead of hiding it

Goal: never again show the user an unexplained 40s spinner.

1. Pass an explicit `retry` policy to `ChatGoogleGenerativeAI`
   (`includes/graph.py:create_model`) — bounded attempts, capped backoff.
2. Wire the SDK-level retry path so `_notify_retry`
   (`includes/agents/base.py`) actually reaches the user — either by lowering
   the SDK retry count so our wrapper sees the error, or by adding a transport
   interceptor.
3. Add a per-turn wall-clock budget in `run_turn`
   (`includes/chat/runner.py`): on breach, stream a clear "the model is busy,
   please retry" message and stop.
4. Make `llm_call_with_retry` (`includes/email_pipeline.py`) back off on the
   *same* model's 429 instead of immediately retrying it — or skip straight to
   the fallback model.

**Acceptance:** simulate 429s locally; user sees a retry notice within ~5s and
a definitive failure message instead of an indefinite spinner.

---

### Phase 3 — Reduce round-trips

1. **Rule-based fast path in the supervisor** for unambiguous RFQ mutations:
   "add \<supplier\> to line \<n\>", "remove supplier…", "set qty…" — pattern
   match before calling `classify_intent`. Saves the 2.3s classifier call and
   removes one 429 exposure.
2. Consider collapsing agent calls #1 and #3 where a tool's result is already a
   complete answer (the `manage_rfq` response includes a summary the model
   restates).

**Acceptance:** `classify_intent` is not called for messages matching the
pattern list (assert via logs); turn makes ≤2 model calls for the target flow.

---

### Phase 4 — Reduce payload weight

1. **Split the `manage_rfq` action catalogue.** 23.2k chars / 40% of all tool
   schema for one tool. Options:
   - Slim `manage_rfq` to the 6-8 common actions; move the rest behind a
     grouped tool or a discoverable `manage_rfq_help(action=...)`.
   - Move `department_prompt_table()` out of the description into its own
     `list_departments` tool (small win — only 1,578 chars — but removes a
     table from every call).
2. Trim `config/prompts/procurement_agent.md` (15.8k chars) or split it so the
   RFQ-specific section only loads on RFQ-active threads.
3. Add **Vertex context caching** for the static prefix (system prompt + tool
   schemas ~18k tokens). Cuts both latency and quota consumption per call.

**Acceptance:** measured input tokens per call down ≥40% (10,428 → <6,300); new
tool-count/description budget asserted in a test so it can't silently regrow.

---

### Phase 5 — Deterministic fast path for "add supplier" (Q4)

Goal: the most common staff action shouldn't involve the LLM at all.

1. `_add_supplier_sync` / `_add_suppliers_to_line_core`
   (`includes/tools/rfq_crud.py`) already do everything: name → `supplier_id`
   matching, contact merge, pricing enrichment, near-miss flagging, history.
   A non-LLM caller completes the whole operation in **~0.3s**.
2. Add a chat/dashboard widget: type-ahead over `search_suppliers`, pick a
   line, one click to add. Wire it like the existing RFQ action buttons
   (`includes/chat/rfq_actions.py` → `RFQ_ACTIONS`, `includes/tools/action_tools.py`).
3. **UI library:** per the repo's Preline policy, build any new UI from Preline
   components (`data-hs-*`), not new Alpine/Tailwind widgets. Flag for review.

**Acceptance:** adding a supplier to a line completes in <1s end-to-end with no
model call; still writes the same JSONB shape + history entry the LLM path
produces.

---

## Measurement harness

Reusable scripts left in the repo root from the diagnosis (read-only):

| Script | Purpose |
|---|---|
| `_diag_agent_speed.py` | static payload size + single-call latency |
| `_diag_add_supplier_tools.py` | tool timings (`search_suppliers`, DB match) |
| `_diag_turn_speed.py` | real graph turn via `FakeChatContext` (read-only prompt) |
| `_diag_model_latency.py` | model A/B with identical payload |

Production log access (CLI already linked from the repo root):

```bash
railway logs --lines 4000 --json > /tmp/ea_logs.json
grep -c "429 Too Many Requests" /tmp/ea_logs.json
grep -E "\[TIMING\]|\[TOOL\]|Supervisor routing" /tmp/ea_logs.json
```

Baseline to beat (RFQ-2026-1054, 2026-09-23): **60.8s** with 3 × 429 retries.

**Add a test** that asserts the tool-schema token budget, so Phase 4 can't
silently regress.

---

## Non-goals

- Reworking the agent/supervisor architecture.
- Changing the RFQ tool *behaviour* (only its schema size and call frequency).
- The unrelated `rfq_items.suppliers` dangling-`supplier_id` incident
  (RFQ-2026-2111 500) — tracked separately; see
  `/memories/repo/rfq-dangling-supplier-id.md`.
