# Plan: Real-Time Activity UI & Messaging Foundation

**Created:** 2026-09-30
**Status:** 🟡 Proposed — awaiting scheduling decision
**Scope:** Single-user activity visibility, real-time push
**Related docs:** `docs/CHAT_UI.md`, `docs/AGENT_BRIDGE.md`

---

## Summary

The agent runs several long "pipeline" flows — create-RFQ item extraction, the
supplier-finding pipeline (classify → validate → group → search), quote
application, and admin background jobs. During a run the user gets:

- A **chat transcript** of what happened (durable, but only messages).
- A **single optimistic header spinner** (`agentWorking` in `templates/base.html`).
- An **RFQ-scoped banner** driven by the `rfqs.pipeline_activity` JSONB lock.

What is missing is a *structured, real-time* view of work in progress: which
steps of a pipeline have completed, how long each took, and what is running
right now across the user's threads. The user asked for two things:

1. **Per-session pipeline checklist** — expandable at the top of a conversation,
   showing completed / running / pending steps with timings, scoped to the
   thread/RFQ.
2. **Global activity badge** — a header indicator that expands to a live list of
   all currently running activities, with navigation back to each thread/RFQ.

The UI itself is the easy part. The real work is that **there is no canonical
model of a run or its steps anywhere in the system**, and **there is no
server-push channel that is not tied to a single thread**. This plan introduces
both, then builds the two UI surfaces on top.

**Decisions already made (from discussion):**
- Scope: **the user's own runs only** for now (no cross-user/admin feed).
- Delivery: **real-time push**, not polling, even though it is more work.
- Approach: introduce a small, swappable **activity registry + event channel**
  rather than bespoke events per feature — but stop short of a full external
  message broker (Postgres LISTEN/NOTIFY is the intended durable backend, and
  is deferred to a gated final phase).

---

## Current architecture (ground truth)

| Concern | Where | Nature |
|---|---|---|
| Chat transport | `includes/chat/context_sse.py`, `chat_ui.stream` | Per-thread `asyncio.Queue` → per-thread SSE. Single-consumer, no replay beyond a 300s grace window. |
| Live runs | `_active_runs` in `includes/dashboard/routes/chat_ui.py` | In-memory `thread_id → {queue, task}`. **No metadata** (no RFQ, label, step, started_at). |
| Busy/lock/cancel | `includes/chat/runner.py` (`_run_locks`), `includes/agent_bridge.py` (`_cancel_events`, `_running_tasks`) | In-memory, process-local. |
| RFQ pipeline lock | `rfqs.pipeline_activity` JSONB (`includes/tools/rfq_creation_pipeline.py`, `includes/dashboard/routes/rfqs.py`) | Single current `step` string + heartbeat. Polled via `data-poll-url`. |
| Supplier pipeline stage | `rfqs.pipeline_stage` string, set by `_set_pipeline_stage_sync` (`includes/tools/supplier_search_tools.py`) | Status marker only; values `unprocessed/classified/validation_gate/validated/grouped/suppliers_internal/awaiting_web_search/complete`. |
| Header badge | `agentWorking`, `_workingThreads` Alpine state in `templates/base.html` | Browser state, raised optimistically on click; cleared by DOM `CustomEvent`s from the chat embed. |
| Dashboard commands | `SseChatContext.notify_dashboard()` → `dashboard:*` DOM events | Works only when the relevant thread's embed/stream is mounted in this tab. |

Two structural gaps follow from this:

1. **No run model.** `pipeline_activity` has one step; `_active_runs` has none.
   A checklist cannot be rendered from either.
2. **No user-level channel.** Every push path is per-thread and only reaches the
   tab that currently has that thread open. A global badge has nowhere to
   subscribe.

---

## Goals

- A single, queryable model of an **Activity** (a run) with ordered **Steps**.
- A **user-level, real-time event channel** that any dashboard surface can
  subscribe to, independent of which thread is open.
- The two UI surfaces described above, built on that model.
- No regression to existing behaviour: chat streaming, the RFQ lock, stop/cancel
  and busy semantics must keep working throughout.

## Non-goals (this plan)

- Cross-user / admin-wide activity feeds.
- A full external broker (Redis, RabbitMQ, Kafka).
- Durable replay of every chat token (the transcript remains the message record).
- Multi-replica correctness — designed for, but implemented in the gated final
  phase, not before.

---

## Design decisions

- **Activity is a first-class record, not a string.** `{id, user_email,
  thread_id, rfq_number, agent, kind, label, status, steps[], started_at,
  finished_at, error}`; each step `{key, label, status, started_at,
  finished_at, detail}`.
- **One registry interface, in-memory first.** `ActivityRegistry` is defined as
  an interface with an in-memory implementation. A Postgres-backed
  implementation (Phase 6) is a swap, not a rewrite.
- **Publish/subscribe, per-user topics.** An `EventBus` abstraction; the
  in-memory bus fans out to subscribed async queues. The user-level SSE endpoint
  is its first consumer.
- **Sync→async bridge is explicit.** The create-RFQ pipeline runs in a
  `threading.Thread`; the supplier pipeline runs on the event loop. Any bus must
  marshal cross-thread publishes via `loop.call_soon_threadsafe`. This is a
  known sharp edge and gets its own task.
- **Additive, feature-flagged rollout.** New endpoints and events are additive;
  existing `dashboard:*` DOM events and `/active-runs` polling remain until the
  new channel proves itself.
- **Preline for all new UI.** Badge drawer, checklist and progress affordances
  use vendored Preline components (`data-hs-*`) per the project UI standard.

---

## Phase 1 — Unified Activity Model

### 1. Define the Activity and Step data model
- Add a transport-neutral module (e.g. `includes/chat/activity.py`, or
  `includes/activity/model.py`) defining `StepStatus`
  (`pending|running|done|failed|skipped`), `Step`, `ActivityStatus`
  (`running|awaiting_input|succeeded|failed|cancelled`), and `Activity`.
- Provide `to_dict()`/`from_dict()` with strict JSON-safety (reuse the
  `_json_safe` discipline from `context_sse.py`).
- Keep it dependency-light — no FastAPI, no DB, no transport imports.

### 2. Define the ActivityRegistry interface and in-memory implementation
- Interface: `create`, `get`, `update`, `start_step`, `finish_step`,
  `mark_step`, `complete`, `fail`, `list_for_user`, `snapshot`.
- In-memory impl keyed by activity id, with an index by `user_email` and by
  `thread_id`. Thread-safe for the sync-thread case.
- Bounded retention: finished activities are retained for a short grace window
  (mirroring `_FINISHED_RUN_GRACE_SECONDS`) then pruned lazily, like
  `_prune_finished_runs`.
- Unit tests for lifecycle transitions and pruning.

### 3. Seed the registry from existing run paths
- In `run_turn` (`includes/chat/runner.py`) create an Activity per turn and
  complete it at the end (success/fail/cancel), so chat turns appear in the same
  model as pipelines.
- In `chat_ui.dispatch_action_to_thread` / `_execute_action` create an Activity
  for action runs, carrying `action_name` and any `rfq_id` from the payload.
- Ensure the registry knows the `thread_id` so the checklist can be resolved for
  the open conversation.

### 4. Reconcile with `_active_runs`, `_run_locks` and cancel state
- Make `_active_runs` (or a thin wrapper over it) derive its "is this running"
  answer from the registry where practical, so there is one source of truth.
- Keep the cancellation registry in `agent_bridge.py` as-is for now, but record
  the cancel reason on the Activity when a run is stopped.
- Document the ownership boundary in a module docstring so a future phase can
  migrate the lock to Postgres without archaeology.

---

## Phase 2 — Event Bus & User-Level SSE Channel

### 5. Define the EventBus abstraction
- Interface: `publish(topic, event)`, `subscribe(topic) -> AsyncIterator`,
  `unsubscribe`. Event envelope: `{type, activity_id, thread_id, user_email,
  payload, at}`.
- Topics are per user (`user:{email}`); scoping and ownership are enforced by
  the endpoint, not by trusting the client.
- Event types at minimum: `activity_started`, `activity_step`,
  `activity_updated`, `activity_finished`.

### 6. Implement the in-memory EventBus with a sync→async bridge
- Fan out to per-subscriber `asyncio.Queue`s with a bounded size and
  drop-oldest-with-a-marker policy (a slow dashboard must not stall a run).
- Provide `publish_threadsafe(...)` that resolves the running loop once at
  startup and marshals via `loop.call_soon_threadsafe`, for the create-RFQ
  `threading.Thread`.
- Unit tests covering loop-bound publish, cross-thread publish, and slow
  subscriber behaviour.

### 7. Wire the ActivityRegistry to publish events
- Registry mutations (`create`/`start_step`/`finish_step`/`complete`) publish
  the corresponding events on the owner's topic.
- Keep registry and bus decoupled via a callback/listener list so Phase 6 can
  swap the bus without touching the registry.

### 8. Add the user-level SSE endpoint
- `GET /chat-ui/events` in `includes/dashboard/routes/chat_ui.py` (or a sibling
  router): authenticated via `require_user`, subscribes to `user:{email}`,
  streams events with the same keep-alive and `X-Accel-Buffering: no` headers as
  the existing thread stream.
- Include an initial `snapshot` event (current activities for the user) so a
  newly connected tab is immediately correct, followed by deltas — this is what
  removes the need for optimistic client state.
- Support `Last-Event-ID` to resume from a monotonic sequence where feasible;
  document the retention window so a client knows when to fall back to
  `snapshot`.

### 9. Retire the optimistic-only badge path (behind the new channel)
- Keep `dashboard:agent_working` / `dashboard:agent_done` DOM events for
  backward compatibility, but make the header badge's *source of truth* the
  `snapshot` + deltas from `/chat-ui/events`.
- Remove the "raise badge optimistically on click" behaviour once the server
  emits `activity_started` promptly enough; leave the local safety timeout as a
  fallback only.

---

## Phase 3 — Pipeline Step Instrumentation

### 10. Instrument the create-RFQ pipeline with ordered steps
- Extend the `_set_rfq_pipeline_activity` boundary calls in
  `includes/tools/rfq_creation_pipeline.py` to drive `Activity.start_step` /
  `finish_step` instead of only a single current-step string.
- Stage boundaries already exist (`extracting_items`, `adding_items`,
  `updating_details`) — map them to named steps with timings derived from
  `started_at`/`heartbeat_at`.
- Publish through `publish_threadsafe` (this runs off-loop).

### 11. Instrument the supplier-finding pipeline
- Drive step transitions from the stage markers currently written by
  `_set_pipeline_stage_sync` in `includes/tools/supplier_search_tools.py`
  (`classified`, `searching`, `complete`) and the richer `rfq.pipeline_stage`
  values (`validated`, `grouped`, `suppliers_internal`,
  `awaiting_web_search`).
- Define the canonical ordered step list for this pipeline in one place so both
  the checklist and any future progress bar render from the same source.
- Confirm the resume path before wiring it: `_resume_pipeline_from`
  (`includes/chat/rfq_actions.py`) calls `agent._run_pipeline_from_stage`, which
  **is not defined** anywhere in the repo. Resolve (implement or delete) as part
  of this task — see Risks.

### 12. Reconcile `pipeline_activity` (the RFQ lock) with the Activity model
- Keep `rfqs.pipeline_activity` as the dashboard **lock** (it is load-bearing
  for read-only enforcement), but derive its `step` from the Activity/registry
  so the banner and the checklist never disagree.
- Preserve the stale-heartbeat unlock semantics in `_rfq_pipeline_active`.

### 13. Include background jobs in the model
- Adapt `includes/chat/job_progress.py` (`monitor_job`) to open an Activity and
  record its lifecycle, so admin script jobs appear in the same surfaces.
- Decide whether periodic output snippets remain chat messages (recommended:
  yes — the transcript is the output record) while the checklist shows coarse
  status and timing.

---

## Phase 4 — Per-Session Pipeline Checklist (Idea 1)

### 14. Checklist component and rendering
- Build a collapsible checklist at the top of the chat panel (Preline
  disclosure/accordion or an Alpine-free Preline component — do not mix Alpine
  and Preline on one tree).
- Render: step label, status icon (done / running / pending / failed / skipped),
  duration, and running elapsed time.
- Collapsed by default when idle; auto-expand while a run is active; auto-collapse
  a few seconds after completion (with the summary line still visible).

### 15. Data source and scoping
- Resolve the Activity for the currently open thread from the registry (via a
  small endpoint such as `GET /chat-ui/threads/{id}/activity`, or the snapshot
  in the events stream).
- RFQ-bound threads show the RFQ pipeline; plain threads show the chat turn's
  tool steps. Decide the fallback when no Activity exists (render nothing).

### 16. Timings and history
- Show per-step durations and total elapsed. A finished checklist should remain
  viewable in the transcript/thread after the run ends, not vanish with the run.
- Decide persistence: the in-memory grace window is enough for the live view;
  if a completed checklist must survive reload, persist the Activity summary
  (Phase 6 makes this durable).

### 17. Tests and manual verification
- Extend `tests/test_chat_ui_routes.py` for the activity endpoint.
- Add a client-side harness test (see `tests/client/`) for checklist rendering
  across step states.
- Manual E2E: run a create-RFQ pipeline and the supplier pipeline; confirm the
  checklist advances in real time via the events channel with no polling.

---

## Phase 5 — Global Activity Badge & Drawer (Idea 2)

### 18. Header badge subscribes to `/chat-ui/events`
- Replace the browser-state badge in `templates/base.html` with a component
  driven by the snapshot + deltas.
- Show a live count (e.g. "2 running") and the most recent activity label;
  clicking opens the drawer instead of immediately stopping.
- Keep a **Stop** affordance per item inside the drawer (the current
  click-to-stop on the badge is moved there).

### 19. Activity drawer list
- Preline dropdown/overlay listing the user's running (and recently finished)
  activities: RFQ number/title where bound, agent, current step, elapsed,
  progress fraction.
- Each row navigates to the owning thread/RFQ using the existing
  `chat-ui:open-rfq` / `openThreadById` navigation — reuse, do not reinvent.
- Empty state and a "show finished" toggle; cap the finished list length.

### 20. Stop / cancel from the drawer
- Each running row gets a stop control wired to the existing
  `POST /chat-ui/threads/{id}/stop` (which already targets only that run).
- Reflect the resulting `activity_finished` event immediately in the drawer.

### 21. Reconciliation and reload behaviour
- On page load and on reconnect, the `snapshot` event must fully rebuild the
  badge and drawer — no stale optimistic entries.
- Keep `/chat-ui/active-runs` as a cheap fallback for older clients and as a
  cross-check; document its relationship to the events channel.

### 22. Tests
- Server: snapshot correctness, ownership scoping (a user must never see another
  user's activities), and event fan-out.
- Client: drawer render, navigation dispatch, stop dispatch, reconnect/snapshot
  rebuild.
- Regression: existing `dashboard:agent_working` / `agent_done` consumers in
  `templates/base.html` and `templates/chat_ui/embed.html` still behave.

---

## Phase 6 — Durability & Multi-Replica Readiness (gated)

> **Gate:** do not start until either (a) the app runs more than one replica, or
> (b) completed activity history must survive a deploy/restart. Until then the
> in-memory implementation is correct and sufficient.

### 23. Persist activities and steps
- Alembic migration adding `activities` and `activity_steps` tables (UUID id,
  user_email, thread_id, rfq_number, kind, label, status, timestamps, JSON
  detail).
- Postgres-backed `ActivityRegistry` implementing the same interface; select it
  via config so local/dev can stay in-memory.

### 24. Replace the in-memory bus with Postgres LISTEN/NOTIFY
- Publish events to a channel; each replica runs a listener and fans out to its
  locally-connected SSE subscribers — cross-replica delivery with no new infra.
- Keep the sync-thread publisher path working through the same abstraction.

### 25. Migrate the run lock and cancel registry
- Move `_run_locks` to a Postgres advisory lock keyed on `thread_id` (already
  flagged in `runner.py`).
- Decide whether `_cancel_events` / `_running_tasks` also move, or whether stop
  becomes a DB flag polled by the runner.

### 26. Replay and history
- Define the retention/replay contract now that events are durable; expose
  persisted Activity history for the checklist's "finished" view.

---

## Testing strategy

- New unit tests under `tests/` (registry lifecycle, bus, sync bridge) and
  `tests/chat/` (activity endpoints).
- Extend `tests/test_chat_ui_routes.py`, `tests/test_agent_bridge.py`.
- Client harness in `tests/client/` for checklist + drawer rendering.
- Guard action coverage: `tests/test_action_coverage.py` must still pass — any
  new button needs a handler.
- Manual E2E per `docs/TESTING.md`, using `scripts/test_rfq_creation.py` for the
  create-RFQ path (NetSuite writes intercepted by default).

## Risks & open questions

- **`_run_pipeline_from_stage` is missing.** `rfq_actions._resume_pipeline_from`
  calls a method that does not exist in the codebase. Either the resume path is
  dead or it is resolved dynamically. This must be settled before Phase 3 step
  instrumentation of the supplier pipeline is trustworthy.
- **Sync-thread publishing** is the sharpest edge; get the bridge right in
  Phase 2 and test it explicitly.
- **Single-consumer SSE vs multiple tabs.** A user with several tabs each
  subscribing to `/chat-ui/events` must all receive events — the per-user topic
  fans out, unlike the single-consumer thread queue. Verify no queue-stealing.
- **Event volume.** Token-level events must stay on the thread stream; the
  activity channel carries only lifecycle/step events.
- **Retention vs reload.** Without Phase 6, a reload mid-run recovers state from
  the snapshot but loses per-step history beyond the grace window. Acceptable
  for the single-user live view; call out in the UI if needed.
- **Preline/Alpine boundary.** The drawer and checklist must be one or the
  other, not mixed, per the project UI rule.

## File map

- **New:** `includes/chat/activity.py` (model + registry + bus, or split into
  `includes/activity/`), `tests/chat/test_activity_registry.py`,
  `tests/chat/test_event_bus.py`, `tests/chat/test_activity_endpoints.py`.
- **Edited (server):** `includes/chat/runner.py`,
  `includes/dashboard/routes/chat_ui.py`, `includes/chat/rfq_actions.py`,
  `includes/tools/rfq_creation_pipeline.py`,
  `includes/tools/supplier_search_tools.py`, `includes/chat/job_progress.py`,
  `includes/dashboard/routes/rfqs.py`, `includes/agent_bridge.py`.
- **Edited (UI):** `templates/base.html`, `templates/chat_ui/embed.html`
  (and/or a new `templates/chat_ui/_activity_checklist.html`), plus
  `input.css` + rebuilt `public/tailwind.min.css`.
- **Phase 6:** `alembic/versions/<new>.py`.
