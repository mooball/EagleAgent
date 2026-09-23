# Plan: LLM Observability, Model Registry & Failover

**Created:** 2026-09-24
**Status:** 🟡 Scoped — P0 targeted for production tonight
**Branch:** `llm-benchmarks`
**Related:** `plan-agentTurnLatency.prompt.md`, `scripts/bench_gemini_flash.py`

---

## Summary

We have almost no visibility into how our LLM calls actually behave in
production, model selection is spread across ~15 environment-variable reads
across two different SDKs, and the existing 429 failover is **broken** — it
falls back to a model that returns 404.

This plan covers seven stated problems, which collapse into **three
workstreams**: observe, then control, then choose.

**Tonight's scope (P0)** is the observation half plus three cheap fixes that
reduce 429s at the source. It is deliberately small: no UI, no registry, no new
abstractions. The registry and failover are designed here but built next.

| Problem (from user) | Workstream | Phase |
|---|---|---|
| Little visibility into speed / accuracy | Observe | **P0** |
| Better 429 / similar error handling | Repair + Control | **P0** (repair), P2 (failover) |
| Regions affect availability | Measure | **P0** (measured — global-only for 3.x) |
| Keep an eye on costs | Observe | **P0** (capture), P4 (report) |
| Switch models without restart | Control | P1 + P4 |
| Run benchmarks any time | Choose | P3 |
| Room for non-Gemini providers | Control | P1 (schema only) |

---

## Evidence already gathered (2026-09-24)

### 1. The 429 fallback is dead

`includes/email_pipeline.py:25` → `FALLBACK_MODEL = "gemini-2.0-flash"`.
Tested against our project today:

```
404 NOT_FOUND — Publisher model .../locations/global/publishers/google/models/gemini-2.0-flash
was not found or your project does not have access to it
```

The same string is `SUPERVISOR_MODEL`'s default in `config/settings.py:74`.

`llm_call_with_retry` therefore does: primary → **retry the same overloaded
primary** → fall back to a model that does not exist. Under 429 pressure this
is strictly worse than no failover, because it looks handled. This is a bug
fix, not a feature.

### 2. There are two call paths, ~15 model-resolution sites

| Path | Used by | Model source |
|---|---|---|
| `ChatGoogleGenerativeAI` (LangChain) | `agents/base.py`, `browser_agent.py`, `general_agent.py`, `procurement_agent.py`, `graph.py::create_model`, `chat/rfq_actions.py:462` | `Config.get_agent_model()` |
| `genai.Client` (raw SDK) | `email_pipeline.py`, `intent_classifier.py`, `quote_tools.py`, `rfq_crud.py` (6 sites), ~10 `scripts/` | `get_pipeline_model()`, `Config.DEFAULT_MODEL` |

Any per-call setting (region, model, capability gate) has to be honoured in
**both**. This is also the natural seam for a future second provider.

### 3. `location` is ambient, never per-call

Only `GOOGLE_CLOUD_LOCATION` (env, `global`) appears anywhere. There is no
per-call region control in the codebase, so **region failover does not exist
today in any form**.

### 4. Region is NOT observable, and Gemini 3.x is effectively global-only

Two measurements on 2026-09-24, both of which change the design.

**(a) The serving region cannot be logged.** On the `global` endpoint the
response carries no region information at all — checked `model_version`
(returns the bare model name), `response_id` (opaque), and every response
header (no `location`/`region`/`gfe`/backend hint). All that is ever exposed is
the location **we asked for**; error messages echo it too
(`.../locations/us-central1/publishers/...` on a 404).

⇒ On `global` we log the region we *requested* (`global`, a router), never the
region that actually served the call. **There is no way to attribute a bad
call to an upstream region.**

**(b) Gemini 3.x is available almost nowhere except `global`.**

| region | 2.5-flash (GA) | 3.5-flash | 3.5-flash-lite | 3.6-flash | 3.8-flash |
|---|---|---|---|---|---|
| `global` | ok | ok | ok | ok | ok |
| us-central1 / us-east1 / us-east4 / us-east5 / us-west1 | **ok** | 404 | 404 | 404 | 404 |
| europe-west1 / europe-west4 | **ok** | 404 | 404 | 404 | 404 |
| asia-east1 | – | 404 | 404 | – | 404 |
| asia-southeast1 | **ok** | ok | 404 | – | 404 |
| australia-southeast1 | – | ok | 404 | – | 404 |

The split is clean: **GA models (2.5) are broadly regional; every Gemini 3.x
model is `global`-only** apart from two odd single-model exceptions.

**Consequences:**

- **Region failover is not available for the models we want to use.** There is
  no regional endpoint to fail over to for 3.6/3.7/3.8-flash. This is why we
  are on `global`, and it is not a choice we can revisit without moving to 2.5.
- **The failover unit that actually exists is the model, not the region**
  (3.8 → 3.6 → 3.5-flash-lite, all on `global`).
- Treat region as an **availability attribute** of a `(model, location)` pair —
  something the registry records and the resolver checks — **not** as a
  failover axis.
- If more quota headroom is the real goal, **a second GCP project is the
  viable lever** (separate quota pool), not a second region. Worth exploring in
  P2 rather than assuming regions are the answer.

### 5. Capability differentiation among current candidates is thin

Probed 2026-09-24 (vision / Google-Search grounding / structured output /
function calling):

| model | vision | grounding | structured | tools |
|---|---|---|---|---|
| gemini-3.5-flash | ✅ | ✅ | ✅ | ✅ |
| gemini-3.5-flash-lite | ✅ | ✅ | ✅ | ✅ |
| gemini-3.6-flash | ✅ | ✅ | ✅ | ✅ |
| gemini-3.7-flash | ✅ | ✅ | ✅ | ✅ |
| gemini-3.8-flash | ✅ | ✅ | ✅ | ✅ |
| gemini-3.1-pro-preview | ✅ | ✅ | ✅ | ✅ |
| gemini-3.1-flash-lite | ✅ | ✅ | ✅ | ✅ |
| gemini-2.5-flash | ✅ | ✅ | ✅ | ✅ |
| gemini-2.5-flash-lite | ✅ | ✅ | ✅ | ✅ |

**Every candidate supports every modality we use.** The only capability
difference found so far is thinking levels:

| model | MINIMAL | LOW | MEDIUM | HIGH |
|---|---|---|---|---|
| 3.5-flash / 3.5-flash-lite / 3.6-flash | ✅ | ✅ | ✅ | ✅ |
| **3.7-flash / 3.8-flash** | ❌ 400 | ✅ | ✅ | ✅ |

Two conclusions:

- **The API exposes no machine-readable capability metadata.** `models.get()`
  returns `supported_actions=None`, `input_token_limit=None`, etc. Capabilities
  must be **probed** or **declared by hand**. A probe harness is required, and
  its results cannot be inferred.
- Capability gating will **not** constrain much today — it matters for
  **future non-Gemini providers**, where vision and grounding genuinely vary.
  Build the field into the schema now; expect it to bind later, not now.

> **Lesson from building the probe (worth keeping):** a naive probe reported
> `gemini-2.5-flash` as having **no vision**. The real error was
> `400 INVALID_ARGUMENT: Provided image is not valid` — our 1×1 test PNG was
> degenerate. The 3.x models tolerated it; 2.5 did not. **A capability probe
> must validate its own fixture first, and must distinguish "unsupported" from
> "bad input".** A wrong capability claim is worse than no claim, because the
> router will silently exclude a working model.

### 6. Server is single-process

`start.sh` runs `uvicorn main:app` with **no `--workers`**. Runtime model
switching therefore does not need Redis or pub/sub — a DB-backed table read
with a short cache is sufficient. Revisit if we ever scale out.

### 7. Measured baseline (thinking=LOW, `scripts/bench_gemini_flash.py`)

| model | mean wall | $/1k calls |
|---|---|---|
| **3.5-flash-lite** | **1.20s** | **$0.53** |
| 3.6-flash | 3.10s | $2.28 |
| 3.5-flash | 3.32s | $5.89 |
| 3.8-flash | 3.74s | $1.43 |
| 3.7-flash | 7.01s | $1.53 |

All five scored 10/10 on the department-grouping task. Full detail in
`/memories/repo/gemini-model-benchmark.md`.

### 8. Service tiers exist — and Priority is the real answer to 429

Vertex offers **Standard / Priority / Flex** traffic tiers (launched 2026-04-02).
Confirmed working on our project:

| tier | what it gives us |
|---|---|
| **Priority** | highest criticality — requests are **not preempted at peak load**. Overflow past your Priority limit is **served at Standard instead of failing** (graceful downgrade). Ideal for interactive chat. |
| **Flex** | **~50% cheaper** than Standard, synchronous, but slower and less reliable. Google's docs name "agentic workflows where the model browses or thinks in the background" as the target case. |
| Standard | what we run today |

**Mechanism (verified):** the installed SDK (google-genai 1.68.0) has **no**
`service_tier` field — `GenerateContentConfig` rejects it. It must go through
`HttpOptions`:

```python
config.http_options = types.HttpOptions(
    extra_body={"service_tier": "SERVICE_TIER_PRIORITY"}
)
```

Valid values are the **proto enum names** — `SERVICE_TIER_PRIORITY` (1) and
`SERVICE_TIER_FLEX` (2). Lowercase `"priority"`/`"flex"` are rejected with
`400 Invalid value at 'service_tier' (v1beta1.ServiceTier)`. The Vertex-native
header `X-Vertex-AI-LLM-Shared-Request-Type: priority|flex` is also accepted —
but see the caveat.

**Caveat that matters: on Vertex the serving tier is NOT reported back.**
`x-gemini-service-tier` (documented for the Gemini API) is absent — response
headers are byte-identical for standard, priority and flex requests. So **the
graceful downgrade is invisible to us**: we can log the tier we *requested*,
never the tier that *served* the call. Attribute downgrades indirectly
(latency distribution, or Cloud Billing SKU), or ask the account team whether an
observability signal exists.

Also: Priority requires a **Tier 2/3 paid project** — **confirmed 2026-09-24: we are
Tier 3**, so Priority is unblocked and needs no upgrade request.

### 9. Google's own retry guidance contradicts our implementation

From the official retry-strategy doc (updated 2026-09-22):

- **Global endpoint: "Recommended for availability. The global endpoint routes
traffic dynamically, which can reduce the need for client-side retries caused
by regional capacity issues."** ⇒ direct confirmation that staying on `global`
is right and that region work was the wrong direction (Evidence §4).
- **Real-time / chat: "Fail fast. Limit the number of retry attempts so users
are not left waiting indefinitely for a response."**
- Priority tier: retry with exponential backoff, but check you are not exceeding
quota. Flex tier: **do not retry aggressively** — increase the timeout instead.
- The SDK already retries transient errors **up to 4 times, ~1s initial delay,
up to 60s max delay**, by default.

**Our code does the opposite of the chat guidance.** `llm_call_with_retry`
retries the same model twice (1s/2s), then the SDK adds up to 4 more with
backoff to 60s, then we fall back to a dead model. That is precisely the
"endless spinner" symptom in `plan-agentTurnLatency.prompt.md`. Retry settings
are configurable via `types.HttpRetryOptions(initial_delay, attempts, exp_base,
max_delay, jitter, http_status_codes)`.
### 10. Service tier does NOT change speed in a quiet period

`scripts/bench_gemini_flash.py --tier standard,priority,flex`, gemini-3.5-flash,
thinking=LOW, 5 runs/cell over two tasks:

| tier | mean wall | TTFT | p90 (group_parts) |
|---|---|---|---|
| flex | 3.14s | 2.94s | 12.03s |
| priority | 3.26s | 3.02s | 5.16s |
| standard | 3.38s | 3.09s | 19.12s |

Means are within ~7% — **no measurable difference**. Notably Flex was *not*
slower, despite being documented as a lower-priority tier.

The p90 column hints at the real story (priority 5.16s vs standard 19.12s) but
n=5 cannot support that claim, and it is not consistent across tasks.

**Conclusion: this experiment cannot answer the question it was set.** Priority's
value proposition is *not being preempted under load* — a property that only
appears when the platform is contended. A quiet-period benchmark measures the
median, which is exactly the statistic tier choice does not move.

To actually settle it, one of:

1. **Prod telemetry (preferred).** ``llm_call_log`` already records
   ``service_tier``, ``latency_ms`` and ``error_class``. Run Priority at a
   partial rollout and compare latency *distributions* per tier on real traffic.
   This is the strongest signal and costs nothing extra to collect.
2. **Induced-load experiment.** Add a ``--concurrency`` option to the harness,
   push until 429s appear, and compare tier behaviour under pressure. The only
   way to test the "not preempted" promise offline.

Note also that Flex's documented guidance is "**do not retry aggressively;
increase the timeout instead**", which argues against a Flex→Priority escalation
strategy built on fast timeouts. Escalating *tiers* on failure is still coherent
(Flex first for background work, Priority when it fails), but the timeout needs
to be generous, and it should not be applied to interactive chat where a
multi-minute Flex response is worse than an error.
---

## Tonight's scope (P0)

**Goal:** be able to answer "which model and region are misbehaving right now,
and what did it cost?" — and stop the two worst self-inflicted causes of 429s.

### P0.1 — Structured per-call LLM telemetry

A single sink that records **every** LLM call from **both** call paths.

**New module** `includes/llm/telemetry.py`:

- `record_llm_call(...)` — best-effort, non-blocking, never raises into the
  request path.
- Two thin adapters, one sink:
  - **raw SDK**: wrap `client.models.generate_content` / `_stream`.
  - **LangChain**: a `BaseCallbackHandler` on `ChatGoogleGenerativeAI`, since
    we cannot wrap the agent's invocation directly.

**Fields** (the useful subset, not everything we *could* capture):

| field | why |
|---|---|
| `ts`, `scope` (`agent:procurement`, `pipeline:QUOTE/extract`, `supervisor`) | slice by workload |
| `provider`, `model`, `location` | the join key for "which model/region is bad" |
| `latency_ms`, `ttft_ms` | speed |
| `prompt_tokens`, `output_tokens`, `thought_tokens` | cost, and thinking drift |
| `attempt`, `fell_back_from` | did failover fire, and did it help |
| `status` (`ok`/`error`), `error_class`, `http_status` | separate 429 from 500 from 404 |
| `correlation_id` | `thread_id` / `rfq_number` when available, to stitch a turn together |

**Storage:** new `llm_call_log` table (Alembic migration) **plus** a structured
log line, so Railway logs and SQL both work. We already run Alembic on deploy.

**Guardrails:** writes are fire-and-forget with a bounded queue; a telemetry
failure must never fail a user request. Add a `scripts/prune_llm_call_log.py`
following the existing `prune_checkpoints.py` pattern.

*Why a table and not just logs:* P4 (the admin view) and "which model/region is
problematic" are both aggregate questions, which is SQL's job, not grep's.

**Effort:** ~half a day. **Risk:** low — additive, no behaviour change.

### P0.2 — Repair the failover path

Touching `llm_call_with_retry` and `Config`, so fix all of it at once:

1. `FALLBACK_MODEL` → a model that exists (`gemini-3.5-flash-lite`), and make it
   read from env rather than being a hardcoded string.
2. **Stop retrying the same model twice.** Today: primary, primary, dead-model.
   Instead walk a *distinct* candidate list.
3. **Fail fast for interactive turns** (Evidence §9). Cap total attempts so a
   user is never left on a spinner for 60s. Keep longer budgets for background
   work only.
4. **Honour `Retry-After`** when the API sends it, instead of a fixed 1s/2s ramp,
   and add jitter.
5. **Move the retry policy into `types.HttpRetryOptions`** rather than our own
   loop, so the SDK's ~4 attempts and ours stop compounding.
6. **Set `service_tier`** (Evidence §8): `SERVICE_TIER_PRIORITY` for
   interactive scopes — its graceful downgrade is what turns a hard 429 into a
   slightly-slower success — and `SERVICE_TIER_FLEX` for background scopes.
7. Audit `config/settings.py` for other dead default model strings
   (`SUPERVISOR_MODEL` defaults to `gemini-2.0-flash`) and confirm what prod
   actually has set — `.env` locally overrides it, prod may not.

**Effort:** ~2–3 hours (larger than first estimated — tier plumbing is new).
**Risk:** medium. This is the hot path, and `service_tier` changes cost and
latency characteristics. Priority entitlement is confirmed (Tier 3), but we
still cannot *observe* which tier served a call on Vertex (Evidence §8), so a
downgrade would be silent — treat Priority as a hypothesis to validate with
P0.1 telemetry, not a guaranteed fix.
Needs `tests/test_email_pipeline.py` retry tests updated and extended.

### P0.3 — Take the background sync off the agent's model

`main.py` starts **three** in-process background loops (`_gmail_sync_loop`,
`_netsuite_sync_loop`, `_maintenance_loop`, at lines 49/84/114), each sleeping
on `GMAIL_SYNC_INTERVAL` / `NETSUITE_SYNC_INTERVAL` / `MAINTENANCE_INTERVAL`.
The email pipeline resolves to `Config.DEFAULT_MODEL` — **the same model the
chat agent uses**. Our own background work competes with live chat turns for
the same quota, from the same process.

Introduce a distinct `SYNC_MODEL` (default `gemini-3.5-flash-lite`, which is
also the cheapest) and resolve the sync path through it. Audit all three loops,
not just the two sync ones. This reduces 429s **at the source** rather than
routing around them.

**Better still, now that we know tiers exist (Evidence §8):** run the sync path
on `SERVICE_TIER_FLEX`. Flex is ~**50% cheaper** and explicitly intended for
latency-tolerant background work — the sync loops wait 300s between runs and
nobody is watching, so slower completion is fine. That turns the contention fix
into a cost saving at the same time. Note Flex's own guidance: **do not retry
aggressively, raise the timeout instead.**

**Effort:** ~1 hour, mostly finding every resolution site.
**Risk:** low-medium — must not accidentally change agent or user-facing
pipeline models. Cover with a test asserting the resolution order.

### P0.4 — Add `--location` to the harness and re-baseline on `global`

Add `--location` to `scripts/bench_gemini_flash.py` (needed regardless — the
registry records location, and the harness must be able to exercise a
`(model, location)` pair).

Originally this was "measure whether regions differ so we know whether to build
region failover". Evidence §4 answers the main question already, so the
remaining value is narrower and still worth an hour:

- **Per-model 429 rate and latency on `global` over time** — because model
  failover is the mechanism we are actually going to build, and the registry
  needs real per-model health data to order its candidates.
- **Latency from Australia.** `australia-southeast1` and `asia-southeast1` do
  serve `gemini-3.5-flash` (though not `-lite`). Pinning might beat global
  routing for us specifically. Unknown until measured.
- **Confirm the availability matrix is stable**, not a transient allowlist
  state. Re-run §4's matrix and diff.

**Effort:** ~1 hour + run time. **Risk:** none — read-only measurement.

### P0.5 (stretch, only if P0.1–P0.4 land cleanly)

Surface throttling to the user instead of hiding it. `langchain_google_genai`
retries at INFO level below our logging, so the user sees an endless
"Agent working…" spinner with no explanation. A `_notify_retry` hook that emits
a chat status line ("upstream is throttling — retrying") converts a mystery
hang into an understandable wait.

**Explicitly out of scope tonight:** the model registry, automatic failover,
the admin UI, capability-based routing, non-Gemini providers, cost reporting.

---

## Follow-on phases (designed, not built)

### P1 — Model registry

One table becomes the single source of truth, replacing the scattered env reads:

```
llm_targets
  id
  scope        # 'agent:procurement' | 'pipeline:QUOTE/extract' | 'supervisor' | 'sync'
  priority     # walk order for failover
  provider     # 'google'  (schema ready for others)
  model        # 'gemini-3.6-flash'
  location     # 'global' | 'us-central1'
  caps         # ['vision','grounding','structured','tools']  -- required capabilities
  thinking     # preferred thinking level, or null
  enabled
```

Resolution returns an **ordered candidate list**, filtered by required
capabilities. That single abstraction serves runtime switching without restart,
failover, per-task model choice, future providers, and gives telemetry its key.

**`caps` is the answer to "not all models support vision or grounding".** A
scope declares what it *needs*; the resolver only offers models that satisfy it.
For current Gemini models this filter is a no-op (see §5), but it becomes load
bearing the moment a non-Gemini provider or a `-tts`/image-only model is added,
and it makes an accidental bad assignment impossible rather than merely
unlikely. Seeded from the probe in §5, not hand-typed.

### P2 — Automatic failover

Consumes P1. On `429`/`503`/`504`/`404`, walk to the next candidate. Keep
`Retry-After` honoured, add a circuit breaker so a persistently failing target
is skipped for a cool-off window rather than being retried on every request.

**Scope correction from Evidence §4:** this is **model failover on `global`**,
not region hopping. Region is an availability attribute of a
`(model, location)` pair, not a failover axis — for Gemini 3.x there is no
second region to hop to.

Worth exploring instead, if quota headroom is the goal: **a second GCP project**
as a separate quota pool. That is a genuinely different lever and should be
assessed before any region work is revived.

### P3 — Benchmark tooling with regions

Incremental on what exists: `--location`, capability matrix mode, and the
"tasks as data" refactor so a task declares its prompt (loaded from
`config/prompts/` so it cannot drift from production), its cases, its grader,
and its **required capabilities**.

**Important framing:** benchmarks measure accuracy on fixtures *we wrote*.
Real accuracy comes from production telemetry — logging the model's output
*alongside the outcome* (did staff accept the department? correct the part
number? re-run it?). That is why P0.1's `correlation_id` matters: it is what
lets us attach an outcome to a call later. Benchmarks for capability,
production telemetry for quality.

### P4 — Admin UI

Thin view over P1, added as a section in the existing
`includes/dashboard/routes/admin.py` (which already carries `require_admin` and
`_render`): list scopes, show resolved target + candidate order, edit
model/region/enabled, and a health panel driven by P0.1's telemetry (latency,
error rate, 429 rate, cost per scope). No new state — the UI is a window onto
the registry. The existing job-runner UI is a likely reuse for on-demand
benchmark runs.

---

## Risks

| Risk | Mitigation |
|---|---|
| Telemetry writes slow the hot path | fire-and-forget queue; never in the request's critical path; measure before/after |
| Telemetry table grows unbounded | prune script + index on `ts` |
| P0.2 changes behaviour of the retry loop | existing retry tests extended first; `Retry-After` honoured but capped |
| P0.3 accidentally changes a user-facing model | explicit test on resolution order; sync model asserted distinct from agent model |
| A capability probe mis-reports and silently excludes a working model | probe must validate its fixture; unknown ≠ unsupported; failures recorded, not assumed |
| Region work is built on an unproven premise | **Measured 2026-09-24** — Gemini 3.x is `global`-only, so P2 is model failover, not region hopping |

## Open questions

1. Does prod actually set `SUPERVISOR_MODEL`, or is it quietly on the dead
   `gemini-2.0-flash` default?
2. Are 429s happening *now*, or were they a bad window on 2026-09-23?
3. ~~Is there an existing admin page + auth pattern to follow?~~ **Resolved:**
   `includes/dashboard/routes/admin.py` already exists (users, system admin,
   job runner, NetSuite status) and exposes `require_admin` + `_render` from
   `._helpers`. P4 should add a section there rather than invent a new area —
   and the existing job-runner UI is likely reusable for triggering a
   benchmark run by hand.
4. What retention do we want on `llm_call_log`? (Suggest 30 days initially.)

## Verification for tonight

- `uv run pytest tests/ -x --timeout=60 -q --no-header --ignore=tests/agents/test_browser_agent.py`
- New: telemetry unit tests (both adapters, and that a sink failure does not
  propagate into the caller)
- New: failover test asserting the candidate list is distinct and that a 404
  target is not retried
- New: resolution test asserting `SYNC_MODEL` ≠ agent model
- Manual: run a chat turn locally, then
  `SELECT model, location, status, count(*) FROM llm_call_log GROUP BY 1,2,3;`
  and confirm rows appear for both call paths
- Prod: confirm the deploy logs rows, and re-check the harness region results
