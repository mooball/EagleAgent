# Plan: Chat Migration — Final Leg

> Parent: [plan-chatMigration.prompt.md](plan-chatMigration.prompt.md)
> Predecessor: [plan-chatMigration-beta.prompt.md](plan-chatMigration-beta.prompt.md) (VALIDATED)
> Acceptance record: [parity-checklist-chat.md](parity-checklist-chat.md)
> Status: **IN PROGRESS** (2026-09-07). P1 ✅, P2 ✅, Fix 1 ✅, Fix 2 planned.
> Scope: everything between "beta validated" and "Chainlit deleted".

---

## Governing principle

**Every phase below ships behind `CHAT_UI_BETA_USERS` except the last one.**
Shared files are touched only by *branching*, never by replacing the Chainlit
path. The kill switch (clear the env var) stays intact until Phase 7.

The two shared touch points in this whole plan are:

| File | Discriminator |
|---|---|
| [templates/base.html](../../templates/base.html) | `document.getElementById('agent-iframe')` — present = Chainlit user |
| [includes/agent_bridge.py](../../includes/agent_bridge.py) | request hint + allowlist check, else fall through to the existing path |

> ⚠️ **Regression precedent.** The `/api/latest-thread` → `/chat-ui/current-thread`
> swap (fixed in `733aac6`) broke non-beta users because a shared call site was
> *replaced* rather than *branched*. Every phase ends with the non-beta scan in
> the "Definition of done" below.

**Definition of done (every phase):**
1. `uv run pytest tests/ -q --no-header --timeout=60 --ignore=tests/agents/test_browser_agent.py` green.
2. Inline `<script>` blocks in touched templates pass `node --check`.
3. Grep the diff for beta-gated endpoints (`/chat-ui/…`) called from shared code paths.
4. Manual smoke as a **non-beta** user: dashboard loads, iframe resumes latest thread, one RFQ action button works.

---

## Current gap inventory (verified 2026-09-07)

| # | Gap | Severity | Phase |
|---|---|---|---|
| 1 | Dashboard action buttons (C-A1–9) can't reach the new UI | Blocker | P1 ✅ |
| 2 | Chat-emitted action buttons (C-B) discarded by `SseChatContext.say()` | Blocker | P2 ✅ |
| 3 | `ctx.get/set` scratch dies with the run (Chainlit's persists per session) | Correctness | P3 |
| 4 | No checkpoint resume backfill in the new UI | Correctness | P4 |
| 5 | Welcome messages, `ctx.image()`, system actions, rename, timestamps… | Polish | P5 |
| 6 | Everyone still on Chainlit by default | Rollout | P6 |
| 7 | Chainlit code still present | Cleanup | P7 |

---

## P1 — Transport-neutral action dispatch (the one architectural piece)

> **Status: DONE (2026-09-07).** `dispatch_action_to_thread` lives in
> `includes/dashboard/routes/chat_ui.py` rather than a new `includes/chat/dispatch.py`
> — it shares `_active_runs` and `_cancel_key`, so locality beats the original
> file layout. `handle_bridge_request` routes on `body.chat_ui` + allowlist +
> `payload._thread_id`; `_sendAction` sends the hint and fires
> `chat-ui:action-started`; the embed opens the stream on that thread. The
> Chainlit path is untouched. Tests: `tests/test_bridge_dispatch_sse.py`.

**Why it's smaller than it looks:** all 21 handlers in `RFQ_ACTIONS`
([includes/chat/rfq_actions.py:1067](../../includes/chat/rfq_actions.py)) already
have the signature `async def handler(payload: dict, ctx: ChatContext)`. The
*only* Chainlit dependency is **context construction**.

### Hook points

**A. `includes/agent_bridge.py:172` — `dispatch_action(session_id, …)`**
Lines ~189–206 are the coupling:
```python
from chainlit.context import init_ws_context
from chainlit.session import WebsocketSession
session = WebsocketSession.get_by_id(session_id)
init_ws_context(session)
ctx = ChainlitChatContext.from_session()
```
Leave this function **entirely untouched**. Add a sibling instead.

**B. NEW `includes/chat/dispatch.py`**
```python
async def dispatch_to_thread(user, thread_id, action_name, payload) -> dict
```
- Verify ownership via `transcript.get_thread(thread_id, user["email"])`.
- Per-thread lock, mirroring `_session_locks` at [agent_bridge.py:34](../../includes/agent_bridge.py).
- Build `SseChatContext(thread_id=…, user_email=…, agent="eagle", queue=…, cancel_key=f"chat-ui:{thread_id}")`.
- Resolve the handler in the same order `dispatch_action` uses:
  `RFQ_ACTIONS` → `includes.chat.actions.get_action` (skip the `@cl.action_callback`
  registry — those are Chainlit lifecycle actions, handled in P5).
- Run it inside `with chat_context(ctx):`.

**C. `includes/dashboard/routes/chat_ui.py:35` — `_active_runs`**
Currently only populated by `post_message` ([:758–771](../../includes/dashboard/routes/chat_ui.py)).
Dispatch must register the same shape `{"queue": Queue, "task": Task}` so that:
- the busy check at [:739](../../includes/dashboard/routes/chat_ui.py) serialises actions against messages, and
- `stream()` at [:776](../../includes/dashboard/routes/chat_ui.py) can drain the events.

**D. `includes/dashboard/routes/chat_ui.py:776` — `stream()` race**
```python
run = _active_runs.get(thread_id)
if run is None:
    yield _sse("done", {})   # ← returns immediately
```
The panel must attach *after* the run is registered. Chosen approach:
**dispatch POST returns 200 only once the run is registered**, and the client
opens the stream on that response. (Alternative — create the queue on demand in
`stream()` — is rejected: it would leak queues for threads that never run.)

**E. `templates/base.html:1194` — `_sendAction(action, refreshAfter)`**
Already the single choke point for all 9 dashboard actions; already injects
`action.payload._thread_id` from `[data-dashboard-context]`. Branch it:
```js
if (document.getElementById('agent-iframe')) { /* existing POST, unchanged */ }
else { /* POST /api/agent-bridge with {chat_ui: true}, then open the stream */ }
```
The `ensure-rfq-bound` and `agent-working` CustomEvents fire in **both** branches.

**F. `includes/agent_bridge.py:265` — `handle_bridge_request`**
Today it 400s when `X-Chainlit-Session-id` is absent (~:287). New order:
1. auth (unchanged);
2. if `body.get("chat_ui")` **and** the user is on the allowlist **and**
   `payload._thread_id` is present → `dispatch_to_thread(...)`;
3. else → existing cookie + `dispatch_action(...)` path, verbatim.

### Tests
`tests/test_agent_bridge_dispatch.py` (new): routing chooses SSE vs Chainlit;
non-allowlisted user with `chat_ui: true` falls through to the Chainlit path;
unowned `thread_id` → 404; busy thread → 409.

### Risk
**HIGH** — these handlers mutate real RFQ data. Handlers themselves are not
modified. Verify the `rfq_identify_items` payload shapes (checklist D2: C-A2
all-items vs C-A3 single-line) before shipping.

---

## P2 — Chat-emitted action buttons (C-B)

**Every emit site funnels through one protocol method**, so this is one change,
not thirteen. `ActionSpec` is `(name, label, payload, tooltip)` —
[includes/chat/context.py:37](../../includes/chat/context.py).

Emit sites:
| File | What |
|---|---|
| [includes/chat/supplier_search_gate.py:42–73](../../includes/chat/supplier_search_gate.py) | Supplier-search menu (5 buttons — treat as ONE component, checklist D3) |
| [includes/chat/rfq_actions.py:403–419](../../includes/chat/rfq_actions.py) | Pipeline web-search prompt + "No thanks" |
| [includes/chat/job_progress.py:27–36](../../includes/chat/job_progress.py) | Cancel job |
| [includes/tools/job_tools.py:52–69](../../includes/tools/job_tools.py) | run_script confirm / cancel |
| [includes/chat/actions.py:140](../../includes/chat/actions.py) | Help / action menu |

### Hook points
1. **`includes/chat/context_sse.py:~110`** — `say()` now serializes `ActionSpec`s
   via `_serialize_actions()` and emits them in the `message_start` event payload as
   `actions: [{name, label, payload, tooltip}]`, and persists them into the step
   `metadata` (`transcript.get_steps` parses the JSON metadata back out) so they
   survive a reload.
2. **`templates/chat_ui/embed.html`** — buttons render under the bubble via
   `renderActions()` (wired into `message_start` and `renderHistory` from
   `step.metadata.actions`); `sendAction()` POSTs to the endpoint and re-opens
   the event stream on success; `setRunning()` disables action buttons while a
   run is live.
3. **`POST /chat-ui/threads/{id}/action`** in `chat_ui.py` → `dispatch_action_to_thread`
   from P1. One dispatch layer serves both directions.

**Done** — buttons on historical messages stay live (decision: yes). Tests:
`tests/chat/test_sse_context.py` (actions in message_start + metadata, empty
list when none) and `tests/test_chat_ui_routes.py::TestThreadAction` (400/200/
409).

### Risk
**MEDIUM** — landed on top of P1. Buttons on historical messages stay live.

---

## Fix 1 — Navigation stability while a run is live ✅

**Bug report (2026-09-07):** start a long action on RFQ A, navigate to RFQ B
via the Chats list — the panel showed A's live stream under B's header, and
navigation froze (clicking back on A did nothing) until a server restart.

**Root causes:**
1. `loadMessages()` bailed whenever `running` — thread switches mid-run never
   rendered the new thread (stale rows stayed, still fed by A's open stream).
2. `dashboard_refresh` re-rendered `window.location.pathname` at dispatch
   time — a run on A re-rendered the page for B the user was viewing, and the
   context identity flip-flop re-navigated the panel (snap-back loop).

**Fixes (client-only):**
- `embed.html` tracks `runThreadId`/`streamThreadId`; `showThread()` clears the
  pane on switch; `loadMessages()` only skips the running thread's own live
  view; stream handlers render only when their thread is on screen (stream
  stays open so `done` is never missed); stop button shows only on the running
  thread; `chat-ui:navigate` with `fresh` responds while locked.
- `dashboard:` events carry `_source_thread`; `base.html` scopes
  `dashboard_refresh` to the emitting thread's page.

---

## Fix 2 — Multi-run concurrency (3–4 simultaneous RFQ tasks) — PLANNED

**Goal:** staff run several RFQ/thread actions at once and navigate freely.
The server already supports this (per-thread `_active_runs`, per-thread 409
busy checks, replay-on-connect queues); only the client models one global
`running` flag and one visible stream.

**Work items:**
1. **Per-thread run state** — replace the global `running` boolean with
   `runs = {thread_id: true}`. Composer / stop / action buttons enable or
   disable per active thread; `sendAction`/`send` currently bail globally.
2. **Single visible stream, switched on view** — leaving a thread closes its
   EventSource (its queue buffers server-side; the run continues); returning
   reconnects and replays. Browser limits (~6 HTTP/1.1 connections) make one
   stream safer than N open ones. Remove the Fix 1 guard in
   `chat-ui:action-started` that ignores a second run.
3. **`loadMessages` per-thread** — guard becomes `runs[tid] && streamThreadId === tid`.
4. **`GET /chat-ui/active-runs`** — list the user's running thread ids (scan
   `_active_runs`, filter by ownership) so the thread list can show busy
   spinners, and a fresh page load still shows what's running.
5. **Cosmetics** — the shell's `agentWorking` badge stays global (any
   background work); `agent_done` from one run may clear it while another
   runs (acceptable, or track a count later).

**Known server-side assumptions to re-verify:** `setup_globals()` idempotence
under concurrent dispatch, graph checkpointer per-thread isolation (both
appear safe: `pg_pool` max 10, LangGraph per-thread configs).

---

## P3 — Persistent scratch state

**The bug:** `SseChatContext._scratch` is an in-memory dict created per run
([context_sse.py:~98](../../includes/chat/context_sse.py)), whereas Chainlit's
`cl.user_session` persists for the session. Real dependency:
[rfq_actions.py:508–538](../../includes/chat/rfq_actions.py) increments
`pipeline_fixes_{rfq_id}` across *separate button clicks* and resets it at 3.
With per-run scratch the counter never advances → the validation-fix loop
misbehaves.

**Fix:** back `get`/`set` with thread-scoped storage — `threads.metadata`
under a `scratch` key via the existing `transcript` adapter (no migration), or a
small table if metadata churn proves noisy. Keep the in-memory dict as a
write-through cache for the duration of a run.

Also audit `ctx.get("active_graph")` at [rfq_actions.py:969](../../includes/chat/rfq_actions.py) —
Chainlit sets this in `user_session`; the SSE path must supply it explicitly
(resolve via `registry.resolve(agent).graph()`).

### Risk
**MEDIUM** — silent wrong behaviour if missed; cheap to test directly.

---

## P4 — Resume backfill

`plan_resume_backfill(ckpt_messages, existing_steps) -> list[AIMessage]` already
exists and is transport-neutral:
[includes/chat/streaming_logic.py:112](../../includes/chat/streaming_logic.py).

Reference implementation to mirror (do **not** modify):
[app.py:415–470](../../app.py) — `aget_state(graph_config)` → `plan_resume_backfill`
→ persist each missing message as an `assistant_message` step with
`metadata {"recovered_from_checkpoint": True}`.

**Hook:** `GET /chat-ui/threads/{id}/messages` in `chat_ui.py` (and
`_steps_with_files`), before returning steps. Matters after deploys/restarts,
which kill in-flight runs — `_active_runs` is in-process.

### Risk
**LOW–MEDIUM** — reuses proven logic; needs `setup_globals()` for the graph.

---

## P5 — Remaining parity items

| Item | Hook | Notes |
|---|---|---|
| Welcome / welcome-back (B5) | [app.py:290–331](../../app.py), [:480–501](../../app.py) | Per-agent × first-visit variants; new UI shows nothing on new thread |
| `ctx.image()` inline (B10) | [context_sse.py:~149](../../includes/chat/context_sse.py) emits `📸 {name}` only | Persist as an `elements` row (the P1-era attachment writer already does this) and reuse `renderAttachments` |
| `new_conversation` | [app.py:555](../../app.py) + [actions.py:157](../../includes/chat/actions.py) | Registry action — reachable once P1 lands |
| `cancel_job`, `cancel_run_script` | [app.py:561–586](../../app.py) | Chainlit-only callbacks; port into the neutral registry |
| Thread rename in panel | `PATCH /chat-ui/threads/{id}` exists | UI only |
| Auto-naming (B2) | `ctx.rename_thread` implemented | `"{RFQ} — {customer}"` |
| Token footer as data (B7) | [runner.py:451](../../includes/chat/runner.py) builds an HTML `<div>` | Currently re-marked client-side via `.agent-footer`; optional structured metadata |
| Timestamps / copy button / avatars (B14–B16) | `embed.html` | Chainlit freebies, now ours |

### Risk
**LOW** — all additive, beta-surface only.

---

## P6 — Flip the flag (no code change)

1. Set `CHAT_UI_BETA_USERS` to all active users on Railway.
2. Soak **~1 week**. Chainlit stays mounted; rollback = shrink the env var.
3. Sweep whatever P5 items surface in real use.
4. Sign off [parity-checklist-chat.md](parity-checklist-chat.md) §E — this is the
   gate for P7.

---

## P7 — Delete Chainlit (the only irreversible step)

Only after P6 sign-off, and ideally as **one revertable commit**.

| Remove | Notes |
|---|---|
| `app.py` | All `@cl.*` lifecycle handlers |
| `.chainlit/config.toml`, `chainlit.md`, stock locale files | Checklist D7 |
| `public/embedded.js`, `public/stylesheet.css` | iframe glue — checklist D1/A20 |
| `mount_chainlit(...)` + `inject_chainlit_auth` middleware in `main.py` | ~`main.py:561` |
| `X-Chainlit-Session-id` path in `/api/stop-agent` | Beta stop already uses `/chat-ui/threads/{id}/stop` |
| Chainlit branch of `dispatch_action` + `_session_locks` | Keep `notify_dashboard`/stop helpers |
| `includes/chat/context_chainlit.py` | Adapter no longer needed |
| The `{% if user and is_beta_chat_user(...) %}` branch + iframe markup in `base.html` | Embed becomes unconditional |
| `_navigateChat`'s `/chat/thread/{id}` URL scheme | Replace with plain thread ids |

**Keep:** `includes/chat/data_layer.py`, `local_storage_client.py`,
`transcript.py` — the SQLAlchemy data layer and storage client remain the
persistence engine. Update `ALLOWED` in
[tests/test_no_chainlit_imports.py](../../tests/test_no_chainlit_imports.py)
accordingly (that test also asserts every ALLOWED file *does* import chainlit,
so it will fail loudly if the list rots).

Post-cutover (separate commits): tighten `allow_origins` (A17), `steps` schema
tidy-ups (`createdAt` → `timestamptz`, drop Chainlit-only columns).

---

## Deferred ideas (captured, not scheduled)

- **Background-task UX** — runs already survive navigation/tab close (server-side
  `asyncio.Task`, steps persisted). Missing for a full experience: SSE **replay
  on reconnect** (stream currently drains from connect-time only), a
  **dashboard-dirty relay** for `dashboard:*` events emitted while no stream is
  open, and **read/unread cursors** (`chat_ui_thread_reads` table + list badge +
  polling).
- **Pipeline → thread updates** — pipelines post summaries into an RFQ's bound
  threads via `transcript.create_step` (`metadata {"kind": "pipeline_update"}`).
  Appears in both UIs for free. Needs spam control (only meaningful stage
  transitions) and a sync→async bridge for the existing sync notifiers.
- **Thread archiving** — summarise old threads into a durable RFQ artefact.
  Thread delete was removed from the UI 2026-09-06 in anticipation of this.

---

## Open questions

1. Long-running C-A actions: keep the dashboard button as the sole busy
   indicator (today's behaviour), or mirror agent-working in the panel header?
   *Proposed: keep today's behaviour.*
2. C-B buttons on historical messages: render active, or disable on old
   messages? *Proposed: render active.*
3. Does `/api/stop-agent` survive P7, or is `/chat-ui/threads/{id}/stop` the only
   stop path? *Proposed: the latter.*
