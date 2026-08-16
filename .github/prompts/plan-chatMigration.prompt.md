# Plan: Replace Chainlit

> Status: **PROPOSAL — not approved, not started.**
> This document is a design review + migration plan. No code has been written.
>
> Per-phase detail: [Phase 0](plan-chatMigration-phase0.prompt.md) · [Phase 1](plan-chatMigration-phase1.prompt.md)

## Overview

Remove Chainlit (`chainlit~=2.11.1`) as the chat UI **and** as the application's
runtime context, replacing it with purpose-built FastAPI endpoints and a chat UI we
own. The LangGraph agent layer, the Postgres checkpointer/store, and the
Jinja/HTMX/Alpine dashboard all stay.

Two headline findings:

> **1. The chat UI is the easy part. The hard part is that Chainlit is currently
> acting as the application's ambient execution context.** 21 RFQ action
> callbacks, several tools, and the agent base class all reach for
> `cl.user_session` / `cl.context.session` / `cl.Message` at arbitrary depths in
> the call stack. Removing Chainlit means giving that code an explicit context
> object. That refactor is the single biggest de-risking move available.

> **2. The frontend choice does not have to be made now — and should not be.**
> Phases 0–2 (decouple, then build the API) are frontend-agnostic and are required
> under every option. Only after they land do we need to pick between a bespoke
> Alpine/HTMX chat and assistant-ui. The plan is therefore **bespoke-first with
> assistant-ui as a documented contingency**, decided at the Phase 3 gate.

---

## Current Architecture (as-is)

### Process shape

```
uvicorn main:app
  └── FastAPI (main.py)                    ← OAuth, session cookie, dashboard, static
        ├── /                              ← Jinja2 + HTMX + Alpine + Tailwind v3
        ├── /api/agent-bridge              ← dashboard → chat RPC
        ├── /api/rfq-thread                ← RFQ ↔ thread binding
        └── /chat  (mount_chainlit)        ← Chainlit ASGI sub-app (app.py)
              └── socket.io                ← the only chat transport
```

The dashboard renders `<iframe id="agent-iframe" src="/chat">`
([templates/base.html](templates/base.html)). The iframe is **never destroyed**,
only hidden — destroying it kills the websocket session.

### Coupling inventory

| Area | File(s) | Coupling | Notes |
| --- | --- | --- | --- |
| Mount | [main.py](main.py) | **S** | One `mount_chainlit(...)` line |
| Lifecycle hooks | [app.py](app.py) | **M** | `on_chat_start`, `on_chat_resume`, `on_message`, `on_stop`, `header_auth_callback`, `data_layer`, `set_chat_profiles` |
| Streaming loop | [app.py](app.py) | **L** | ~570-line hand-rolled `astream_events(version="v1")` loop. **v1 is deprecated and emits a `LangChainDeprecationWarning` on every turn today.** |
| Data layer | [includes/chat/data_layer.py](includes/chat/data_layer.py) | **M** | `FixedSQLAlchemyDataLayer` patches two upstream bugs |
| Storage | [includes/chat/local_storage_client.py](includes/chat/local_storage_client.py) | **S** | Only inherits `BaseStorageClient` |
| Agent bridge | [includes/agent_bridge.py](includes/agent_bridge.py) | **XL** | `WebsocketSession.get_by_id()` + `init_ws_context()` + in-memory locks |
| RFQ actions | [includes/chat/rfq_actions.py](includes/chat/rfq_actions.py) | **XL** | 1,177 lines, **21** `@cl.action_callback`s, thread-pinning hacks |
| Ambient tool calls | [includes/tools/quote_tools.py](includes/tools/quote_tools.py), [includes/tools/browser_tools.py](includes/tools/browser_tools.py), [includes/tools/job_tools.py](includes/tools/job_tools.py), [includes/agents/base.py](includes/agents/base.py) | **L** | `cl.user_session["active_msg"]`, `cl.context.session.thread_id`, `cl.Image` |
| Iframe glue | [public/embedded.js](public/embedded.js), [templates/base.html](templates/base.html) | **M** | postMessage, DOM-scraping profile detection, sidebar cookie hacks |
| LangGraph layer | [includes/graph.py](includes/graph.py), [includes/agents/](includes/agents) | **~0** | Essentially Chainlit-free. This is good news. |
| Auth | [main.py](main.py) | **S** | FastAPI already owns auth; Chainlit just reads injected headers |
| Tests | [tests/](tests) | **~0** | **No integration test exercises a real Chainlit session.** |

### The load-bearing invariant

> `chainlit.threads.id` == LangGraph `configurable.thread_id` == `rfq_threads.thread_id` == `rfqs.thread_id`

Everything hangs off this UUID. **Preserve it and migration is tractable.
Break it and you orphan every checkpoint and every RFQ binding.**

---

## Review: Answers to the specific questions

### 1. What specifically do we gain?

| Gain | Confidence | Why |
| --- | --- | --- |
| **True concurrent sessions** | **High** | Chainlit binds one `WebsocketSession` per socket, and the *session id is stored in a path-`/` httpOnly cookie* (`X-Chainlit-Session-id`). Two tabs overwrite each other's cookie — hence the existing `visibilitychange` re-POST workaround in [templates/base.html](templates/base.html). Stateless HTTP + SSE keyed on `thread_id` removes the problem by construction. |
| **Death of the iframe** | **High** | No postMessage protocol, no `MutationObserver` scraping `[class*="SelectValue"]` to detect the chat profile, no `iframe.src` reload to switch threads, no `409 thread_already_bound` → reload dance, no theme-sync, no sidebar-cookie clicking. All of [public/embedded.js](public/embedded.js) (293 lines) and most of the postMessage handlers in [templates/base.html](templates/base.html) get deleted. |
| **Death of the server-side session registry** | **High** | `/api/agent-bridge` exists *only* because the dashboard cannot reach into an iframe's websocket. Once the chat is in-page, "dashboard triggers an agent action" is a direct client-side call. `WebsocketSession.get_by_id()`, `init_ws_context()`, `_session_locks`, thread pinning, `_main_pinned()` — all gone. This is the biggest structural win. |
| **Native tool-call UI** | **High** | Today: a transient `cl.Message("⏳ Using {Tool}… (xN)")` that is created, updated, and `.remove()`d, plus a "buffer-and-discard on `on_tool_start`" hack. assistant-ui has tool calls as first-class message parts with per-tool React components (`makeAssistantToolUI`). Strictly better and much less code. |
| **UI freedom** | **High** | Current customisation is CSS overrides that hide Chainlit's own buttons plus JS that clicks them. Everything becomes ours. |
| **Inline / popup toggle** | **High** | Trivial once the chat is an island in our own DOM rather than an iframe. See Q9. |
| **Escape from upgrade churn** | **Medium-High** | Evidence of ongoing tax: `FixedSQLAlchemyDataLayer` patching two upstream bugs, a migration purely to add `steps.autoCollapse` for Chainlit ≥2.10, monkey-patching `chainlit.server.app` middleware, and patching `sio.eio` ping intervals for the Railway proxy. |
| **Cleaner persistence** | **Medium** | We'd own the schema. `threads.createdAt` is a **varchar** today, forcing `"createdAt"::timestamptz` casts in maintenance queries. |
| **Testability** | **Medium** | Plain FastAPI endpoints are testable with `TestClient`. The current `on_message` loop and all **28** action callbacks (21 + 7 in `app.py`) are effectively untestable and untested. |

### 2. What specifically do we lose?

Be honest — Chainlit gives a lot for free:

| Loss | Severity | Mitigation |
| --- | --- | --- |
| Message persistence layer (`threads`/`steps`/`elements`/`feedbacks` + data layer) | **High** | We rebuild it. But we already patch it, and we already reconcile it against LangGraph checkpoints on resume. |
| Thread list / history sidebar, rename, delete, search | **Medium** | assistant-ui `ThreadListPrimitive` + `RemoteThreadListAdapter` (or `ExternalStoreThreadListAdapter`). We write the endpoints. |
| Spontaneous file upload widget + drag/drop + size/type gating | **Medium** | assistant-ui attachment adapter. `accept`/`max_files`/`max_size_mb` move from `.chainlit/config.toml` into our adapter + endpoint. |
| Markdown/code rendering, copy buttons, virtualised scroll, dark mode, autoscroll | **Medium** | assistant-ui ships all of these in its shadcn-style components. Mostly a wash. |
| `cl.Action` button round-trip | **Medium** | Rebuilt as custom message parts + a `run-action` command. See Q3/Q7. |
| `cl.send_window_message` server→client push | **Medium** | Replaced by data parts on the run stream, or a dedicated SSE channel. See Q3. |
| 23 locale translation files | **Negligible** | All stock, unused. |
| Chainlit's built-in auth flow | **Negligible** | FastAPI already owns auth. |
| Feedback (thumbs) adapter | **Low** | `feedbacks` table exists but appears unused. Note: `AssistantTransport` does **not** expose speech/dictation/feedback/suggestion adapters — drop to `ExternalStoreRuntime` if we later want them. |
| **Pure-Python repo** | **Medium-High** *(Option B only)* | The real cost of choosing assistant-ui: Node/Vite/React/TypeScript is a permanent second toolchain, a Docker build stage, and a new dependency/security surface in a repo that today has zero `package.json`. **Option A avoids this entirely** — see "Frontend: decision deferred". |

### 3. The dashboard↔chat bridge — preserve or improve?

**Improve, substantially — by deleting most of it.**

Current, both directions:

```
Dashboard → Chat:
  base.html _sendAction()
    → POST /api/agent-bridge   (auth via eagleagent_session cookie)
      → read httpOnly X-Chainlit-Session-id cookie
      → WebsocketSession.get_by_id(sid)          ← in-process registry
      → init_ws_context(session)                 ← sets contextvars
      → session.thread_id = payload._thread_id   ← thread pinning
      → config.code.action_callbacks[name](Action(...))

Chat → Dashboard:
  notify_dashboard(cmd) → cl.send_window_message
    → socket.io 'window_message'
      → Chainlit frontend window.parent.postMessage(data, "*")
        → listener in base.html
          → agent_navigate | dashboard_refresh | agent_working | agent_done | …
```

After migration:

```
Dashboard → Chat:  same page, same JS context.
  window.EagleChat.runAction({ action, payload })
    → assistant-transport custom command  { type: "run-action", ... }
      → POST /api/chat/threads/{thread_id}/stream
        → server-side action handler, plain async function, explicit ChatContext

Chat → Dashboard:  LangGraph's native `custom` stream channel.
  from langgraph.config import get_stream_writer
  get_stream_writer()({"type": "dashboard_refresh", "payload": {...}})
    → graph.astream(stream_mode=[..., "custom"])
      → React handler drains the custom events
        → calls the *existing* window.navigateDashboard() / window.refreshDashboard()
```

`get_stream_writer()` is verified present in `langgraph~=1.2.5`. It is callable from
inside any tool or node with no plumbing, which makes it a drop-in replacement for
`notify_dashboard()` / `cl.send_window_message()` — and unlike a state field, it does
not pollute `SupervisorState` or the checkpoint.

> **Verified, and easy to get wrong:** LangGraph has **two** non-interchangeable
> custom-event channels.
>
> | Mechanism | `astream_events(version="v2")` | `astream(stream_mode="custom")` |
> | --- | --- | --- |
> | `adispatch_custom_event()` | ✅ as `on_custom_event` | ❌ |
> | `get_stream_writer()` | ❌ | ✅ |
>
> The design above is only valid because Phase 2 streams with `astream(...)`. If any
> code path stays on `astream_events`, it must use `adispatch_custom_event()` instead.

Concrete improvements:
- No `_session_locks`, no cancel-event bypass, no thread pinning, no `_pin_thread()`/`_thread_swap()`/`_send_pinned()`/`_main_pinned()`.
- `postMessage(data, "*")` (wildcard origin) disappears — a real, if low, security improvement.
- The dashboard can target **any** thread explicitly rather than "whichever socket the cookie points at".
- **New requirement:** `/api/chat/*` are cookie-authenticated state-changing POSTs on the same origin. Add CSRF protection (require a custom header + `SameSite=Strict` or a double-submit token). Note `/api/agent-bridge` has this same gap today — worth fixing either way.

**Keep** `window.navigateDashboard()` / `window.refreshDashboard()` and the
`/api/dashboard-context` store as-is. They're transport-agnostic and still useful.

### 4. Concurrent chats for one user — same tab and multiple tabs?

**Yes, in both senses — this is the strongest argument for the migration.**

- **Multiple tabs, different threads:** Works naturally. Each request carries an explicit `thread_id`; the server is stateless per request; state lives in the Postgres checkpointer. No shared cookie to clobber.
- **Multiple threads in one tab:** Works. assistant-ui's thread list runtime holds a thread registry; you can also mount two independent `AssistantRuntimeProvider`s (e.g. inline panel + popup) if we ever want side-by-side.
- **Two tabs on the *same* thread:** Needs deliberate design. Three sub-problems:

  1. **Two simultaneous runs on one `thread_id`** will corrupt the LangGraph checkpoint (this is the same class of bug the existing "dangling tool_call repair" code cleans up). → Add a **per-thread advisory lock** (in-process `asyncio.Lock` keyed by `thread_id`, or Postgres `pg_advisory_lock` if we ever run >1 replica). Second run gets `409 run_in_progress`.
  2. **Tab B should see Tab A's run.** → Options: (a) accept it and require a manual refresh (`aui.threads.reloadMainThread()`); (b) a per-thread SSE fan-out so any tab can attach to an in-flight run. Recommend (a) for v1, (b) as a follow-up — assistant-ui's `onResume` / `resumeApi` hooks exist for exactly this.
  3. **Cancellation** must move from "keyed by Chainlit session id" to "keyed by `run_id`", returned in the stream and stored client-side.

- **Multi-replica caveat:** an in-process run registry only works with a single web process. Railway currently runs one; if that ever changes, cancellation and locks need Postgres/Redis. Flag it, don't build it.

### 5. RFQ ↔ session binding

**Fully preserved, and simpler.** Keep `rfq_threads` and `rfqs.thread_id` exactly as they are.

What changes:
- `_lookup_rfq_thread_id()` in [includes/dashboard/routes/api.py](includes/dashboard/routes/api.py) currently `INSERT`s straight into Chainlit's `threads` table to pre-create a thread. That stays valid if we keep the schema (recommended — see Q11).
- The client-side `_ensureRfqBound()` / `_expectingBoundThread` / `_pendingRfqBind` / `409 thread_already_bound` → `iframe.src = '/chat'` state machine collapses to: *"select thread `X` in the runtime"*. No reload, no race.
- The `#rfq-context-banner` injected by `embedded.js` becomes a normal React component (or stays a Jinja-rendered banner above the chat island).
- The "only bind when `_chatProfile === 'Eagle Agent'`" rule becomes a plain check against the thread's `agent` metadata instead of DOM-scraping a select element.
- **Improvement available:** thread list can be filtered/grouped by RFQ (`GET /api/chat/threads?rfq=RFQ-2026-0042`) and thread titles auto-set server-side, replacing the current `update_thread(name=...)` calls scattered across [app.py](app.py) and [includes/tools/quote_tools.py](includes/tools/quote_tools.py).

### 6. File uploads

**Straightforward.** The processing pipeline is already Chainlit-free.

| Piece | Today | After |
| --- | --- | --- |
| Widget + gating | `.chainlit/config.toml [features.spontaneous_file_upload]` | assistant-ui `AttachmentAdapter.accept` + server-side validation |
| Upload transport | Chainlit socket → temp file → `element.path` | `POST /api/chat/attachments` (multipart) |
| Storage | `LocalStorageClient` → `data/attachments`, URL `/files/{key}` | **unchanged** (drop the `BaseStorageClient` base class, keep the class) |
| Serving | FastAPI `StaticFiles` at `/files` | **unchanged** |
| Processing | [includes/chat/document_processing.py](includes/chat/document_processing.py) — `process_file()`, `create_multimodal_content()` | **unchanged, pure functions** |
| Persistence | `elements` table + the "re-attach elements to a new `cl.Message` to force persistence" hack | Attachment rows written directly. **The hack disappears.** |
| Retention | `_run_maintenance()` in [main.py](main.py), `ATTACHMENT_RETENTION_DAYS` | **unchanged** |

**Must not regress:** `MAX_FILE_SIZE_MB=100`, PDF → `pdfplumber` text + `pypdfium2` render (10 pages @ 2×), xlsx → pandas, images → base64 vision parts.

**Security note:** the new upload endpoint needs explicit MIME sniffing + extension allowlist + size cap enforced server-side (not just in the adapter's `accept`), and must never serve user uploads from the same origin without `Content-Disposition: attachment` / a sandboxed path. Worth auditing the existing `/files` mount at the same time.

### 7. Tool / agent feedback

**This is where the UX gets meaningfully better.**

Today, `cot = "hidden"` in config, no `cl.Step`, no `TaskList`. Tool activity is a
throwaway message; the streaming loop *discards the model's buffered text* when a
tool starts, because that text was reasoning rather than an answer; the token/timing
footer is a raw HTML `<div>` injected via `stream_token` (requiring
`unsafe_allow_html = true`).

After migration:
- Tool calls are first-class parts: name, args, status (`running`/`complete`/`error`), result.
- Per-tool React components via `makeAssistantToolUI` — e.g. `manage_rfq` renders a live RFQ card, `search_products` renders a result table, `browser_tools` renders the screenshot inline instead of a bare `cl.Image`.
- **Per-node attribution for free.** Streaming with `subgraphs=True` gives each chunk a `langgraph_node` / namespace, so the UI can show "ProcurementAgent is working" rather than the anonymous `⏳ Using {Tool}… (xN)`. Our Supervisor → sub-agent topology makes this immediately useful.
- **Generative UI driven from Python.** `push_ui_message()` (`langgraph.graph.ui`, verified present in `langgraph~=1.2.5`) emits a structured UI payload from inside a graph node or tool; it arrives client-side as a `DataMessagePart` rendered by `makeAssistantDataUI`. This is the direct replacement for `cl.Action` + `cl.Message(elements=...)`, and — importantly for this codebase — the payload is defined in **Python**, not TypeScript.
- Reasoning/"thinking" becomes a collapsible part instead of being buffered-and-discarded. The Gemini `thinking`-block skip logic can become a *render* decision rather than a *stream* decision.
- Token/cost footer becomes structured message metadata → `unsafe_allow_html` goes away.
- **Human-in-the-loop uses `langgraph.types.interrupt()`** (verified present). The supplier search gate in [includes/chat/supplier_search_gate.py](includes/chat/supplier_search_gate.py) becomes a real graph interrupt resumed with `Command(resume=...)`, rather than a `cl.Action` round-trip that re-enters `app.main()` with a synthetic message. Interrupts also survive thread switches, restored via the runtime's `load` callback.

**Must be ported (hard-won logic, do not lose):**
- Checkpoint repair — dangling `AIMessage.tool_calls` with no matching `ToolMessage`: ≤2 → inject synthetic error `ToolMessage`s; ≥3 → `RemoveMessage` the corrupt messages.
- The repetition guard (abort if a 30-char snippet repeats 4+ times).
- The three "nothing streamed" fallbacks (`on_chat_model_end` text → `aget_state()` last message).
- Resilient persistence when the client disconnects mid-run.

### 8. The three agents

Chat profiles are a Chainlit concept; the graphs are not. [includes/graph.py](includes/graph.py)
already exposes three independently compiled graphs (`graph`, `research_graph`,
`internal_graph`) sharing `SupervisorState`.

Replacement:
- An `agent` field (`eagle` | `research` | `internal`) stored in `threads.metadata`
  (already JSONB) instead of as an auto-tag. Include it in the run request body so
  the server can resolve `AGENTS[agent].graph`.
- A React agent selector in the composer header. Reuse the existing icon mapping.
- Legacy name normalisation (`"EagleAgent"` / `"System Admin"` → `"Eagle Agent"`,
  which `on_chat_resume` does today) becomes a one-off data migration.
- **Design improvement:** put the registry in one place —
  `includes/agents/registry.py` with `{key, label, description, icon, graph_factory,
  commands, allows_rfq_binding, admin_only}` — instead of the current spread across
  `@cl.set_chat_profiles`, `on_chat_start`, `on_chat_resume`, and `embedded.js`.
- `cl.context.emitter.set_commands()` (the composer command buttons built from
  `INTENTS`/`RESEARCH_INTENTS`) becomes `GET /api/chat/agents` returning the command
  list per agent.
- This also unlocks things that are awkward today: per-agent starter prompts,
  per-agent attachment rules, admin-only agents gated server-side rather than by UI.

### 9. Popup vs inline

**Yes, and it's easy — provided we make the right architectural choice.**

> **Recommendation: do NOT rewrite the dashboard.** Mount the chat as a single
> self-contained *island* (`<div id="eagle-chat-root">`) inside the existing
> Jinja/HTMX/Alpine pages. The dashboard keeps 100% of its current implementation;
> only the `<iframe>` is replaced. This holds for either frontend option.

Given that, placement is pure CSS + a small imperative API:

```js
window.EagleChat.mount({ mode: 'inline' | 'popup' | 'expanded', container });
window.EagleChat.setMode('popup');
```

The existing Alpine panel-state machine (`closed` / `panel` / `expanded`,
`panelWidth`, localStorage persistence) is reused almost verbatim. Because the chat's
state lives in JS rather than in an iframe's document, **it survives being moved
between containers** — impossible with an iframe, where a DOM move reloads it. Popup
mode is `position: fixed` or a real `<dialog>`.

Caveat: a genuinely detached browser popup window (`window.open`) would be a
*separate* client sharing the same `thread_id` — i.e. the "two tabs, one thread"
problem from Q4. Treat that as out of scope for v1.

### 10. Other concerns identified

1. **Test coverage is near zero for the thing being replaced.** No integration test
   touches `on_message`, the streaming loop, or any of the **28** action callbacks. We
   would be refactoring ~2,500 lines of untested orchestration blind. **This is the
   #1 risk.** Mitigation: Phase 0 characterisation tests.
2. **Tailwind / toolchain.** The dashboard uses the **standalone Tailwind v3.4.17
   binary** scanning only `templates/**/*.html`, and per prior experience
   `npx @tailwindcss/cli` (v4) ignores `tailwind.config.js` and strips all styles.
   assistant-ui's components are documented against Tailwind v4, so choosing it means
   **two Tailwind majors on one page** — v4 preflight bleeding into the dashboard,
   plus silent class-name divergence (`shadow-sm` → `shadow-xs`, changed opacity
   syntax) resolved unpredictably by load order. `public/stylesheet.css` already has a
   compiled preflight blob prepended, so this class of problem has bitten us before.
   Shadow DOM would isolate it, but assistant-ui builds on Radix and Radix portals
   escape shadow roots. **This concern is the main reason the frontend decision is
   deferred to the Phase 3 gate.**
3. **Docker / Railway build** *(assistant-ui path only)*. Dockerfile gains a Node
   build stage. Longer builds, `node_modules` in CI, `package-lock.json` to keep
   honest, and a new dependency audit surface (`npm audit` alongside `pip-audit`).
4. **SSE through the Railway proxy.** The repo already had to patch engine.io ping
   intervals for proxy stability. SSE needs `Cache-Control: no-cache`,
   `X-Accel-Buffering: no`, and periodic keep-alive comments. Verify early, on a real
   deploy, not just locally.
5. **SSE connection limits.** HTTP/1.1 caps at ~6 connections per origin. Several tabs
   × (event channel + chat stream) can exhaust it and silently hang. **Confirm Railway's
   edge negotiates HTTP/2**; if not, a `SharedWorker` multiplexing one connection
   across tabs is the fix.
6. **CSRF.** See Q3.
7. **Asset cache-busting** *(assistant-ui path only)*. `/public` is `StaticFiles`; a
   bundle needs a content hash in the filename and a manifest read at startup.
8. **`unstable_` APIs** *(assistant-ui path only)*. `unstable_Provider`,
   `unstable_createMessageConverter`, `unstable_onBranchChange` may change between
   releases. We would be trading Chainlit's upgrade tax for assistant-ui's, not
   eliminating it.
9. **Polish drip** *(bespoke path only)*. Not one hard problem — ~6 medium ones
   (stick-to-bottom autoscroll, incremental markdown across unterminated code fences,
   IME `isComposing` handling, paste/drag upload, streaming layout jitter, stop/error
   states) that arrive over months as "the chat feels janky when I do X". Desktop-only
   scope removes the worst of the tail (mobile keyboards, virtualised scroll). Budget
   for it explicitly rather than assuming polish is free.
10. **Ongoing maintenance surface.** A React/TS codebase in a repo whose conventions
    are Python + `uv` + Jinja, maintained by one developer. Real, permanent cost.
11. **Feature-parity creep.** Chainlit does more small things than it looks like.
    Phase 0 must produce an explicit, signed-off parity checklist, or this project has
    no end.
12. **Stale artefacts to clean up en route:** `chainlit_datalayer.db` (unused SQLite
    at repo root), `chainlit.md` (2 lines, `show_readme_button = false`), 23 stock
    locale files, `public/elements/RFQSummary.jsx` (referenced in
    `copilot-instructions.md` but the folder no longer exists).

### 11. Can we preserve old sessions?

**Yes — and it's much less scary than it looks, because the display layer is not
the source of truth.**

Two independent stores hold conversation state:

| Store | Contains | Owner |
| --- | --- | --- |
| LangGraph checkpoints (`CHECKPOINT_DATABASE_URL`) | The **real** message history the model sees | Us. Untouched by this migration. |
| Chainlit `threads` / `steps` / `elements` | The **rendered transcript** | Chainlit |

`on_chat_resume` already reconciles these two — it backfills missing
`assistant_message` steps from the checkpoint. So even a total loss of `steps` is
recoverable in principle.

**Recommended: Track A — keep the schema, swap the writer.**

- Keep `threads`, `steps`, `elements`, `feedbacks` exactly as they are.
- Write a read adapter: `steps WHERE type IN ('user_message','assistant_message')`
  → `ThreadMessage[]`, ordered by `createdAt`/`start`.
- Write new messages into the same tables, minus Chainlit's quirks.
- **Zero data migration. Zero thread_id churn. Instant rollback** (Chainlit stays
  mounted behind a flag and can still read its own tables).
- Follow-up migrations, once stable: `createdAt` varchar → `timestamptz`; drop
  `autoCollapse` / `command` / `modes` / `waitForAnswer` / `generation` /
  `playerConfig`; rename tables out of the Chainlit namespace.

Track B (new `chat_messages` table + one-off backfill from `steps`) is cleaner
long-term but forfeits the instant-rollback property during the riskiest phase.
Do it *after* cutover, if at all.

**Known lossy edges, all acceptable:**
- Chat profile is currently stored as a thread *tag* (`auto_tag_thread = true`) →
  one-off migration into `threads.metadata.agent`, normalising legacy names.
- The inline HTML token/cost footers are baked into `steps.output` text → old
  messages will render that HTML literally unless we strip it on read. Add a
  sanitising read-side transform (and **never** render historical `steps.output` as
  raw HTML — sanitise or render as markdown-only).
- `elements` rows reference `/files/{objectKey}`; those files are already subject to
  `ATTACHMENT_RETENTION_DAYS` deletion, so some old attachments are already dead
  links. Not a regression.

---

## Recommended architecture (to-be)

### Frontend: decision deferred to the Phase 3 gate

Phases 0–2 are frontend-agnostic. Two candidates, decided once the API exists:

| | **Option A — bespoke (default)** | **Option B — assistant-ui (contingency)** |
| --- | --- | --- |
| Stack | Alpine + `marked` (already loaded) + `EventSource` + DaisyUI | React 19 + Vite + TypeScript + Tailwind v4 |
| New toolchain | **none** | `package.json`, Vite, Node build stage, `npm audit` |
| Tailwind | one major (v3) | **two majors on one page** |
| Polish | ~6 medium tasks, ours to own | largely solved |
| Agent-authored rich UI | Jinja partials pushed over SSE | `push_ui_message()` → `makeAssistantDataUI` |
| Ongoing cost | same idioms as the rest of the repo | permanent second ecosystem |

**Default to A.** The Chainlit surface actually in use is remarkably shallow —
`cot = "hidden"`, no `cl.Step`, no `TaskList`, no custom elements, no `AskFileMessage`.
The whole thing is: message in → streamed markdown out → action buttons → file upload
→ thread list → a transient "using tool" line → one `cl.Image`. Combined with
desktop-only scope and no branching/editing/regeneration, the gap to assistant-ui is
much narrower than it first appears — while the toolchain cost is permanent.

#### Transport notes for Option A

- **Token stream: plain `EventSource` in Alpine, not `htmx-ext-sse`.** htmx's SSE
  extension swaps server-rendered HTML fragments, which cannot render markdown
  incrementally — a half-open code fence needs the *full* accumulated text re-parsed
  each tick. Re-sending the whole bubble per flush works but destroys text selection
  mid-stream. Accumulate tokens in Alpine state and render with `marked`.
- **`htmx-ext-sse` is right for the non-streaming parts:** thread list, history load,
  RFQ banner, action panels — the server-rendered-partial work the dashboard already
  does well.
- Guard incremental markdown against unterminated fences (append a closing ``` before
  parsing when the count is odd).

#### DaisyUI — attractive, but a *separate* project

DaisyUI is a pure-CSS Tailwind plugin (no JS, no React) that ships a **`chat`
component** — `chat`, `chat-start`/`chat-end`, `chat-bubble`, `chat-header`,
`chat-footer`, `chat-image`. That is the message list styled for free, and it brings
theming/dark-mode the dashboard currently hand-rolls. It would benefit the whole app,
not just the chat.

Two obstacles:

1. **Toolchain.** DaisyUI 4.x targets Tailwind v3 (what we have); DaisyUI 5 needs v4.
   The standalone Tailwind binary **cannot load npm plugins**, so either we adopt a
   Node-based Tailwind build (the thing Option A exists to avoid) or we load DaisyUI's
   prebuilt `full.css` from CDN alongside the standalone build. **The CDN route costs
   nothing in build complexity** — just a larger, un-tree-shaken stylesheet.
2. **Class collision — verified.** [input.css](input.css) defines `.btn-sm` (line 9),
   `.btn` (line 33) and `.btn-accent` (line 58) as our own component classes. DaisyUI
   defines all three. They are used across
   [templates/partials/rfq_detail.html](templates/partials/rfq_detail.html),
   [templates/partials/_rfq_comms.html](templates/partials/_rfq_comms.html),
   [templates/partials/_rfq_items_table.html](templates/partials/_rfq_items_table.html)
   and [templates/partials/_smart_item_adder_modal.html](templates/partials/_smart_item_adder_modal.html).
   Adopting DaisyUI restyles every button in the app until ours are renamed
   (`ea-btn`…) or DaisyUI is prefixed.

**Therefore: do DaisyUI standalone and first, or not at all.** Do not bundle a
whole-dashboard restyle into the chat migration.

### If we choose Option B — runtime choice

| Candidate | Verdict |
| --- | --- |
| `useLangGraphRuntime` (`@assistant-ui/react-langgraph`) | **Not as the runtime — but adopt the package as a utility.** See below. |
| `useDataStreamRuntime` | Possible, but message-only. No place for agent state or the dashboard↔chat commands. |
| `useExternalStoreRuntime` | Maximum control, maximum code. Fallback if we need speech/feedback/suggestion adapters. |
| **`useAssistantTransportRuntime`** | **Recommended.** Explicitly designed for "custom agent framework or one without a streaming protocol (e.g. open-source LangGraph)". Ships a **Python** `assistant-stream` package with `append_langgraph_event()` and a FastAPI reference implementation. **Custom commands** map directly onto our 28 RFQ actions. Backend cancellation via `controller.is_cancelled`. |

Trade-off to accept: `AssistantTransport` does not expose speech, dictation,
feedback, or suggestion adapters. It is layered on `ExternalStoreRuntime`, so
dropping down later is possible but is a rewrite of the runtime provider.

#### Why not `useLangGraphRuntime`

It is **not** hard-locked to LangGraph Cloud — `stream`, `create`, and `load` are
our own callbacks, and `unstable_threadListAdapter` takes a plain
`RemoteThreadListAdapter`, so it could be pointed at our FastAPI endpoints. The
reasons to decline are narrower but decisive:

1. **We would have to emit LangGraph Platform's SSE wire format** (`messages/partial`,
   `messages/complete`, `values|<namespace>`, `updates|<namespace>`, `custom`, `error`)
   by hand. That format is owned by LangGraph Platform and is not versioned for
   third-party implementers — we would be impersonating a server we do not control.
2. **No Python-side helper.** `AssistantTransport` ships `assistant-stream` with
   `append_langgraph_event(state, namespace, event_type, chunk)` fed straight from
   `graph.astream(stream_mode=["messages", "updates"], subgraphs=True)`. That is an
   official bridge for exactly our situation.
3. **No custom-command concept.** Everything is a message. Our 28 dashboard-initiated
   RFQ actions map far more cleanly onto `AssistantTransport`'s custom commands.

#### Adopt `@assistant-ui/react-langgraph` as a *utility* dependency

The two are not mutually exclusive. The `AssistantTransport` docs explicitly show
using this package's converter:

```ts
import { unstable_createMessageConverter } from "@assistant-ui/react";
import { convertLangChainMessages } from "@assistant-ui/react-langgraph";

const messageConverter = unstable_createMessageConverter(convertLangChainMessages);
```

Cherry-pick from it:

| Export | Why we want it |
| --- | --- |
| `convertLangChainMessages` | LangChain message → assistant-ui `ThreadMessage`, including tool calls, tool results, and multimodal content parts. `SupervisorState.messages` is already `Annotated[Sequence[BaseMessage], add_messages]`, so this is a direct fit and deletes a chunk of hand-written mapping. |
| `LangGraphMessageAccumulator` + `appendLangChainChunk` | Client-side reassembly of partial chunks into whole messages. Replaces the hand-rolled `_stream_buffer` logic in [app.py](app.py). |

Everything else in the package (`useLangGraphRuntime`, `unstable_createLangGraphStream`)
stays unused.

#### Rejected alternative: self-hosting a real LangGraph server

Running `langgraph-api` as a third process would make `useLangGraphRuntime` the
natural choice. Declined because:

- It takes ownership of threads and checkpoints away from us, breaking the
  `threads.id == thread_id == rfq_threads.thread_id` invariant that the entire RFQ
  binding depends on.
- Our graphs build tools **dynamically per user at runtime** — MCP loading in
  `GeneralAgent.get_tools_async()`, admin-gated tools in `SysAdminAgent` — which fits
  a static `langgraph.json` export badly.
- A third process, a second schema, and another thing to deploy on Railway.

### Target shape

```
uvicorn main:app
  └── FastAPI (main.py)
        ├── /                                  Jinja2 + HTMX + Alpine  (unchanged)
        │     └── <div id="eagle-chat-root">   ← chat island (Alpine, or React if Option B)
        ├── /api/events?topics=...             GET   long-lived per-user SSE (see below)
        ├── /api/chat/agents                   GET   agent registry + commands
        ├── /api/chat/threads                  GET   list (filter: ?rfq=, ?archived=)
        │                                      POST  create
        ├── /api/chat/threads/{id}             GET / PATCH / DELETE
        ├── /api/chat/threads/{id}/messages    GET   history (steps → messages)
        ├── /api/chat/threads/{id}/stream      POST  run → token/tool SSE
        ├── /api/chat/threads/{id}/cancel      POST  cancel run_id
        ├── /api/chat/attachments              POST  multipart upload
        ├── /api/rfq-thread                    (unchanged)
        └── /chat  (mount_chainlit)            ← behind ENABLE_CHAINLIT flag, deleted at the end
```

### The key backend abstraction (Phase 1)

```python
# includes/chat/context.py
class ChatContext(Protocol):
    thread_id: str
    user_email: str
    agent: str

    async def say(self, text: str, *, actions: list[ActionSpec] | None = None) -> MessageHandle: ...
    async def stream(self, token: str) -> None: ...
    async def image(self, data: bytes, *, name: str, mime: str) -> None: ...
    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None: ...
    def get(self, key: str, default=None): ...
    def set(self, key: str, value) -> None: ...
    @property
    def cancelled(self) -> bool: ...
```

Two implementations: `ChainlitChatContext` (wraps today's behaviour, ships first)
and `TransportChatContext` (wraps `assistant_stream.RunController`). Passed
explicitly, or via a `contextvars.ContextVar` we own — **never** by importing
`chainlit`.

**Every `import chainlit` outside `app.py` and `includes/chat/` becomes a lint
error.** That single rule is the whole point of Phase 1.

### Server-push: build the seam now, the feature later

A separate concern — pushing server-originated updates to clients (one user edits an
RFQ, everyone viewing it refreshes; background Gmail/NetSuite sync adds data) — **shares
a transport with the chat and therefore cannot be fully deferred.**

Why it belongs here:

- We already have a crippled version of it. `_notify_rfq_updated()` in
  [includes/tools/quote_tools.py](includes/tools/quote_tools.py) fires
  `dashboard_refresh` on every RFQ mutation — but only down the *acting user's* own
  socket. The capability exists; the fan-out doesn't.
- The background sync loops in [main.py](main.py) have **no** push path at all, because
  there is no chat turn to hang it off. That is the stronger driver.
- It is the same machinery as the "two tabs on one thread" fan-out flagged in Q4. Build
  it chat-only and we build it twice.
- A run stream is **request-scoped** (`POST …/stream` returns that run's events), which
  is useless for "tell me when RFQ-1040 changes". A long-lived channel must be designed
  in Phase 2 or retrofitted painfully.

The seam:

```python
# includes/events.py
async def publish(topic: str, payload: dict) -> None: ...
async def subscribe(topics: set[str]) -> AsyncIterator[Event]: ...

# topics:  user:{email}   rfq:{rfq_number}   thread:{thread_id}
```

- `notify_dashboard()` becomes `publish(f"user:{email}", …)` — identical behaviour today.
- Later, `_notify_rfq_updated()` becomes `publish(f"rfq:{n}", …)` and reaches everyone
  viewing it. One line, because the substrate already exists.
- Background jobs publish to the same topics with no chat context.
- Client: one `EventSource` in [templates/base.html](templates/base.html) dispatching to
  handlers — which **also replaces the entire postMessage bridge**, under either
  frontend option.

**Substrate:** in-process `asyncio` fan-out now; **Postgres `LISTEN`/`NOTIFY`** when we
outgrow one web process. No new infrastructure — and it is the same single-replica
constraint as the run registry and cancellation, so decide it once for all three.

**Payloads are invalidation signals, not data.** Push "RFQ changed, re-fetch" and let
HTMX swap the partial. That is already the `dashboard_refresh` pattern and it is the
robust one.

**Deferred:** the collaboration *feature* — live-updating RFQ views, presence, "Tom is
editing this". Note the honest cheaper alternative: HTMX polling
(`hx-trigger="every 30s"`) on a few partials is adequate for ~10–20 internal users and
needs no architecture at all. We may never need push for the dashboard. But the chat
needs a stream regardless, so the seam is close to free.

**Also needs deciding if we ever want true collaboration:** push tells you data changed,
it does not stop two users clobbering each other. That needs a `version`/`updated_at`
check on `RFQ` writes — cheap now, awkward to retrofit.

### Coexistence with Chainlit during Phases 2–5

**Phase 2 does not break the Chainlit UI.** It is almost entirely additive, and the new
backend can be exercised in production while Chainlit still serves every user.

Verified: `mount_chainlit(app, target="app.py", path="/chat")` is the **last statement**
in [main.py](main.py#L544), and a Starlette `Mount` at `/chat` only matches `/chat/*`.
`/api/chat/*` and `/api/events` therefore have **zero route collision**.

Four things genuinely are shared, and each needs a decision rather than a discovery:

| Shared resource | Risk | Mitigation |
| --- | --- | --- |
| `threads` / `steps` / `elements` tables | New writer emits rows Chainlit's reader chokes on | Populate the columns Chainlit expects (`autoCollapse`, `command`, `modes`, `showInput`, `defaultOpen`). Done right, **the same thread renders in either UI** — the ideal bake-period property. |
| **LangGraph checkpointer, same `thread_id`** | **Two concurrent runs corrupt the checkpoint** — precisely the failure the dangling-tool_call repair exists to clean up | Put the per-thread run lock **inside `run_turn()`** (Phase 1), not in the API layer. Both paths call `run_turn()`, so one lock covers both systems. |
| CSRF middleware | A global check breaks Chainlit's own POSTs (`/chat/set-session-cookie`, the socket.io handshake) | **Scope it** to `/api/chat/*` and `/api/events` only. |
| `pg_pool` (`min_size=1, max_size=10`, [includes/graph.py](includes/graph.py#L38)) | Two consumers plus long-lived SSE connections | Monitor. Likely fine at current user counts; do not assume. |

#### Free win: dual-write `notify_dashboard`

Have `ChainlitChatContext.notify_dashboard()` publish to `events` **as well as** calling
`cl.send_window_message()`. The dashboard can then add its `EventSource` listener during
Phase 2 and validate the event channel against real production traffic, months before
Phase 5 deletes the postMessage path.

#### Testing progression

| Stage | Risk | Proves |
| --- | --- | --- |
| 1. `curl` against `/api/chat/*` on a scratch `thread_id` | **None** — no shared state | Endpoints, streaming, SSE through the Railway proxy |
| 2. New backend on real threads **not open in Chainlit** | Low | Real graphs, tools, persistence |
| 3. Same thread in both UIs | Safe **only after** the lock is in `run_turn()` | Round-trip rendering; the bake-period property |

Stage 1 is exactly the Phase 2 gate, and is achievable with Chainlit fully live.


---

## Dependency posture

Checked 2026-08-16. **No major upgrade is pending** — there is no LangGraph 2.x, and
we are within a patch release of latest on the core packages.

| Package | Installed | Latest | Pin allows? |
| --- | --- | --- | --- |
| `langgraph` | 1.2.9 | 1.2.11 | ✅ patch |
| `langchain` | 1.3.14 | 1.3.15 | ✅ patch |
| `langchain-core` | 1.5.1 | 1.5.5 | ✅ transitive |
| `langgraph-checkpoint-postgres` | 3.0.5 | **3.1.2** | ❌ `~=3.0.5` → `==3.0.*` |
| `langchain-google-genai` | 4.2.2 | **4.3.4** | ❌ `~=4.2.2` → `==4.2.*` |
| `langchain-mcp-adapters` | 0.2.2 | **0.3.2** | ❌ `~=0.2.2` → `==0.2.*` |

The `~=X.Y.Z` convention pins to patch-only, so those three have quietly drifted a
minor behind. Bump them **standalone, before this project starts** — unrelated churn
is the last thing we want landing mid-migration. `langchain-mcp-adapters` 0.2 → 0.3 is
the risky one (0.x minors may break).

### `astream_events`: go to v2 now, skip v3 entirely

Probed empirically against a compiled graph:

| Version | Status |
| --- | --- |
| `v1` (current) | **Live-deprecated.** `LangChainDeprecationWarning: astream_events version='v1' is deprecated. Use version='v2' or astream instead.` |
| `v2` | Default. For the events our loop consumes (`on_chat_model_stream`, `on_chat_model_start`/`_end`, `on_chain_*`) the stream is **identical to v1** — same event counts on an equivalent graph. Documented differences (`parent_ids`, custom events) are things we do not use. |
| `v3` | **`LangChainBetaWarning: The v3 streaming protocol on Pregel is experimental.`** Also a different call convention — `await g.astream_events(...)` returns an `AsyncGraphRunStream` rather than an async iterator. |

**Do the v1 → v2 bump now, as a standalone change**, because it removes a live
deprecation warning and — more importantly — Phase 0's characterisation tests should be
written against the loop we will actually run throughout Phases 1–2, not against a
deprecated API we are about to change underneath them.

**Do not go to v3.** It is beta, and it is the wrong direction: `AssistantTransport`'s
Python bridge consumes `graph.astream(stream_mode=[...], subgraphs=True)`, so **Phase 2
leaves the `astream_events` family entirely**. Investing in v3 is investing in an API
we are about to stop using.

```
now      ───►  astream_events v1 → v2      (small, standalone, kills a live warning)
Phase 2  ───►  astream(stream_mode=[...])   (the actual destination)
v3       ───►  skipped
```

### `create_react_agent` → `create_agent`

Deprecated in its docstring (*"deprecated in favor of `create_agent` from the
`langchain` package"*), no runtime warning yet. Used in
[includes/agents/base.py](includes/agents/base.py#L369). Deferred twice already — in the
[June](.github/prompts/codebaseReview-2026-06-17.md) and
[July](.github/prompts/codebaseReview-2026-07-25.md) reviews — both times as "not a
drop-in replacement".

New angle worth a timeboxed spike in Phase 1: `create_agent` takes a **`middleware`**
parameter. Message trimming, `RemoveMessage` checkpoint cleanup, and the dangling
tool-call repair are all hand-rolled inside `BaseSubAgent` and the streaming loop today,
and middleware is plausibly their proper home. Phase 1 already has `BaseSubAgent` open.
If it still is not viable, defer a third time **with a written reason**.

---

## Migration plan

Sizes are relative effort, not time: **S** < **M** < **L** < **XL**.

> **Detailed per-phase documents:**
> [Phase 0](plan-chatMigration-phase0.prompt.md) · [Phase 1](plan-chatMigration-phase1.prompt.md)
> — files touched, code samples, test plans. Phases 2–6 not yet expanded.

### Prerequisite — do now, independent of this project · **S**

> Landing these before Phase 0 keeps unrelated dependency churn out of the migration.

- [ ] Bump `langgraph-checkpoint-postgres`, `langchain-google-genai`, `langchain-mcp-adapters` to current minors; patch-bump the rest. Run the suite.
- [ ] `astream_events(version="v1")` → `"v2"` at [app.py](app.py#L961).
- [ ] **Verify the tool-call path specifically** — the v1/v2 equivalence check was run on a graph with no tools, so the buffer-discard-on-`on_tool_start` behaviour is unproven across versions.
- [ ] *Optional, independent:* adopt DaisyUI. Rename `.btn` / `.btn-sm` / `.btn-accent` in [input.css](input.css) and all templates to `ea-*`, then load DaisyUI 4 prebuilt CSS. **Own commit, own decision** — do not fold into the migration.

### Phase 0 — Baseline & parity contract · **M**

> Detail: [plan-chatMigration-phase0.prompt.md](plan-chatMigration-phase0.prompt.md)

- [ ] Write an explicit **feature-parity checklist**, signed off before any code. Include every `.chainlit/config.toml` setting we actually rely on.
- [ ] **Characterisation tests** for the untested core, using the existing `cl` mocking patterns from `tests/test_actions.py` / `tests/test_job_tools.py`:
  - [ ] `on_message` streaming loop: buffered-stream-discard, repetition guard, three fallbacks, token accounting.
  - [ ] Checkpoint repair (dangling tool_calls: 0, 1, 2, 3+ cases).
  - [ ] `on_chat_resume` checkpoint↔steps reconciliation.
  - [ ] A representative sample (~8) of the RFQ action callbacks.
  - [ ] Attachment round-trip through `process_file()` / `create_multimodal_content()`.
- [ ] Snapshot prod row counts for `threads` / `steps` / `elements` / `rfq_threads`.
- [ ] **Gate:** tests green and stable before touching anything.

### Phase 1 — Decouple business logic from Chainlit · **XL** ⭐

> Detail: [plan-chatMigration-phase1.prompt.md](plan-chatMigration-phase1.prompt.md)

> Independently valuable. If we stop after this phase, the codebase is materially better.

- [ ] `includes/chat/context.py` — `ChatContext` protocol + `ActionSpec` + `MessageHandle`.
- [ ] `ChainlitChatContext` implementation; wire it in `on_chat_start` / `on_message`.
- [ ] Refactor [includes/chat/rfq_actions.py](includes/chat/rfq_actions.py) (**21** callbacks) to take `ChatContext` explicitly. **Delete** `_pin_thread` / `_thread_swap` / `_send_pinned` / `_main_pinned`; the pinning hacks exist only because `cl.context.session` is ambient and mutable.
- [ ] Refactor ambient callers: `_stream_to_user()` and `manage_rfq()` in [includes/tools/quote_tools.py](includes/tools/quote_tools.py), [includes/tools/browser_tools.py](includes/tools/browser_tools.py) (`cl.Image`), [includes/tools/job_tools.py](includes/tools/job_tools.py), [includes/chat/job_progress.py](includes/chat/job_progress.py), [includes/chat/supplier_search_gate.py](includes/chat/supplier_search_gate.py), [includes/chat/actions.py](includes/chat/actions.py), [includes/agents/base.py](includes/agents/base.py).
  Threading it through `RunnableConfig.configurable` is the cleanest route for tools.
- [ ] Extract the streaming loop from [app.py](app.py) into `includes/chat/runner.py` — `run_turn(graph, inputs, ctx) -> AsyncIterator[Event]` — emitting transport-neutral events. `app.py` becomes a thin Chainlit adapter over it.
- [ ] **Per-`thread_id` run lock inside `run_turn()`.** Placing it here rather than in the Phase 2 API layer means it protects Chainlit-originated runs too — the precondition for testing both UIs against one thread. See "Coexistence with Chainlit".
- [ ] `includes/agents/registry.py` — single source of truth for the three agents.
- [ ] Timeboxed spike: `create_react_agent` → `create_agent`, evaluating whether `middleware` can absorb message trimming, `RemoveMessage` cleanup, and dangling tool-call repair. Defer with a written reason if not viable.
- [ ] Add a CI check: no `import chainlit` outside `app.py` / `includes/chat/`.
- [ ] **Ship this to production on Chainlit and let it bake.**
- [ ] **Gate:** no behaviour change observable to users; Phase 0 tests still green.

### Phase 2 — FastAPI chat backend · **L**

> Frontend-agnostic. Emit **our own** SSE envelope; an assistant-transport adapter is
> added later only if Option B wins at the Phase 3 gate.

- [ ] `includes/events.py` — `publish()` / `subscribe()`, in-process fan-out, topic strings `user:` / `rfq:` / `thread:`.
- [ ] `GET /api/events?topics=…` — long-lived per-user SSE channel.
- [ ] `includes/chat/api/` — the endpoints listed above; auth via the existing `require_user` dependency.
- [ ] `TransportChatContext` streaming from `graph.astream(stream_mode=["messages", "updates", "custom"], subgraphs=True)`. This retires `astream_events` altogether.
- [ ] Reimplement `ChatContext.notify_dashboard()` on `get_stream_writer()` (LangGraph `custom` channel) → `events.publish()` — **not** `adispatch_custom_event()`, which only surfaces under `astream_events`.
- [ ] **Dual-write:** `ChainlitChatContext.notify_dashboard()` publishes to `events` *and* calls `cl.send_window_message()`, so the event channel can be validated against live traffic during the bake.
- [ ] `steps` ⇄ message mapper (read + write), with HTML-footer sanitisation on read. **Writes must stay Chainlit-readable** so a thread renders in either UI.
- [ ] `run_id` registry + cancel endpoint. *(The per-thread run lock lands in Phase 1, inside `run_turn()`.)*
- [ ] Attachment upload endpoint reusing `LocalStorageClient` and `document_processing.py`; server-side MIME/size/extension enforcement.
- [ ] CSRF protection on state-changing `/api/chat/*` routes — **scoped, not global**; a global check breaks Chainlit's own POSTs.
- [ ] SSE hardening: `no-cache`, `X-Accel-Buffering: no`, keep-alives. **Verify on a real Railway deploy, and confirm HTTP/2 at the edge** (connection-limit risk).
- [ ] Endpoint tests with `TestClient`.
- [ ] **Gate:** full conversation drivable end-to-end with `curl` — no frontend at all.

### Phase 3 — Frontend bake-off · **M**

> Build the **same narrow slice twice** on the Phase 2 API: thread list + streaming +
> one tool card + file upload. Decide from two working prototypes, not from argument.

- [ ] **Option A (bespoke):** Alpine component + `EventSource` + `marked`; `htmx-ext-sse` for the non-streaming partials; DaisyUI `chat-bubble` if adopted.
- [ ] **Option B (assistant-ui):** `frontend/` Vite + React 19 + TS, `@assistant-ui/react` + `@assistant-ui/react-langgraph` (converter/accumulator only); `assistant-stream` on the Python side; Dockerfile Node stage; scoped Tailwind v4 pipeline.
- [ ] Both mounted behind a flag at `<div id="eagle-chat-root">`, alongside the existing iframe on a dev-only route.
- [ ] **Kill criteria, set in advance:** abandon B if Tailwind isolation needs shadow DOM or a class prefix hack; abandon A if streaming/scroll behaviour isn't solid within the timebox.
- [ ] **Gate:** pick one. Record the reason. The other becomes the documented contingency.

### Phase 4 — Feature parity · **XL**

- [ ] Thread list + create / rename / archive / delete; RFQ filtering.
- [ ] Thread history load; branching/editing explicitly **out of scope for v1**.
- [ ] Attachments + drag/drop + paste + gating.
- [ ] Agent selector + per-agent composer commands.
- [ ] Tool display: generic fallback + bespoke treatment for `manage_rfq`, `search_products`, browser screenshots.
- [ ] Per-node activity indicator from subgraph namespaces ("ProcurementAgent is working").
- [ ] Reasoning/thinking collapsible; token/cost footer as structured metadata (retires `unsafe_allow_html`).
- [ ] Action buttons → `run-action` → Phase 1 handlers. *Option A:* Jinja partial pushed over SSE. *Option B:* `push_ui_message()` → `makeAssistantDataUI`.
- [ ] Supplier search gate converted to `langgraph.types.interrupt()`, resumed with `Command(resume=…)`.
- [ ] Stop button → cancel endpoint.
- [ ] Inline / popup / expanded modes wired to the existing Alpine panel state.
- [ ] RFQ context banner.
- [ ] Polish pass: stick-to-bottom autoscroll, incremental markdown across unterminated fences, IME `isComposing`, streaming layout jitter, dark mode, code copy.
- [ ] **Gate:** the Phase 0 parity checklist is fully ticked.

### Phase 5 — Bridge v2 & cleanup of iframe glue · **M**

- [ ] Replace `/api/agent-bridge` calls from `base.html` with `window.EagleChat.runAction()`.
- [ ] Single `EventSource` on `/api/events` → existing `window.navigateDashboard()` / `refreshDashboard()`.
- [ ] Delete [public/embedded.js](public/embedded.js), the postMessage listeners, `_ensureRfqBound()`, `_expectingBoundThread`, `_pendingRfqBind`, the `visibilitychange` session-cookie hack, and `/chat/set-session-cookie` usage.
- [ ] Delete `includes/agent_bridge.py` (`WebsocketSession`, `init_ws_context`, `_session_locks`, `_cancel_events`) and `/api/stop-agent`.
- [ ] **Gate:** every dashboard→agent action verified against the Phase 0 checklist.

### Phase 6 — Cutover & decommission · **M**

- [ ] Ship dual-mode to production. `ENABLE_NEW_CHAT` per-user (admin emails first).
- [ ] Bake period with both paths live and Chainlit still able to read the same tables.
- [ ] Data migrations: thread tag → `threads.metadata.agent`; legacy profile-name normalisation.
- [ ] Flip default. Keep the flag for one release.
- [ ] Remove `chainlit` from `pyproject.toml`; delete `app.py`'s Chainlit layer, `.chainlit/`, `chainlit.md`, `chainlit_datalayer.db`, `includes/chat/data_layer.py`, the `BaseStorageClient` inheritance, `OAuthErrorRedirectMiddleware`, the `sio.eio` patch.
- [ ] Follow-up migrations: `createdAt` → `timestamptz`; drop unused Chainlit columns; optionally rename tables.
- [ ] Update [copilot-instructions.md](copilot-instructions.md) and `docs/` (`AGENT_BRIDGE.md` in particular is fully superseded).

---

## Decision gates

Do not proceed past a gate without an explicit go/no-go:

| Gate | Question |
| --- | --- |
| After **Prerequisite** | Did the dependency bumps and the v1 → v2 switch land cleanly, including the tool-call path? |
| After **Phase 0** | Is the parity checklist small enough to be finishable? |
| After **Phase 1** | Did decoupling alone solve enough of the pain? **A legitimate stopping point.** |
| After **Phase 2** | Does SSE survive the Railway proxy under real load, on HTTP/2? |
| After **Phase 3** | Which frontend won, and why? Record it. |
| After **Phase 4** | Genuine parity, or are we shipping a downgrade? |

---

## Open questions for the user

1. **Branching / message editing** — Chainlit has none, and it interacts badly with a LangGraph checkpointer that assumes a linear thread. In scope for v1? *(Recommend: no.)*
2. **Two tabs on the same thread** — accept "manual refresh" for v1, or build the fan-out up front? (The `events.py` seam makes the second cheap.)
3. **Thumbs-up/down feedback** — the `feedbacks` table exists but looks unused. Keep, or drop?
4. **Multi-replica** — will the web process ever scale beyond one? If yes, the run lock, cancellation **and** the event fan-out all need Postgres `LISTEN`/`NOTIFY` rather than in-process state. Decide once.
5. **DaisyUI** — adopt it standalone first (with the `btn` rename), or leave the dashboard styling alone?
6. **Track A vs Track B persistence** — keep Chainlit's table shapes for the cutover (recommended), or migrate to a clean schema up front?
7. **Server-push scope** — build only the `events.py` seam (recommended), or commit to the collaboration feature and `RFQ.version` write-conflict checks now?
8. **Appetite check** — Phase 1 alone is a large piece of work with real benefit and no frontend risk. Commit to Phases 2–6 up front, or ship Phase 1 and re-evaluate?

---

## Bottom line

The diagnosis is correct: Chainlit's single-websocket session model is the direct
cause of the concurrency limits, the iframe is the direct cause of the data-sync
awkwardness, and both are structural rather than fixable. "Do nothing" is the weakest
option — the workarounds are load-bearing and only accrete.

But **this is not a UI migration — it's an inversion of control.** Chainlit is the
application's execution context, and ~2,500 lines of RFQ orchestration are written
against ambient global state with no test coverage. Phase 1 is the actual project.

The frontend question, which felt like the whole decision, turns out to be the *last*
decision — and the least urgent. Phases 0–2 are required under every option and leave
us strictly better off even if we stop there.

Recommendation: **commit to the Prerequisite and Phases 0–2.** Treat Phase 3 as a
timeboxed bake-off with pre-agreed kill criteria, defaulting to the bespoke
Alpine/HTMX build. On a one-developer, pure-Python repo, avoiding a permanent second
toolchain is worth more than the polish a framework buys — but that should be settled
by two working prototypes on a shared backend, not by argument.
