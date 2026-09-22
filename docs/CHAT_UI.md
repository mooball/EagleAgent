# Chat UI

The chat interface is part of the main FastAPI application. There is no
separate chat server, no iframe, and no third-party UI framework: it is
Jinja-rendered HTML with a small amount of vanilla JavaScript, streaming over
Server-Sent Events.

> **History.** This replaced Chainlit. The chat *tables* are still named after it
> (`users`, `threads`, `steps`, `elements`) — they are now plain application
> tables owned by `includes/chat/transcript.py`. A rename migration is optional
> cosmetics; nothing about the behaviour depends on the names.
> Replacing Chainlit also removed the old Chainlit entry point, its data layer,
> its storage client, and the dashboard iframe. Those do not exist on this
> branch.

## Why SSE

Every conversation is a row, and a run is keyed by its thread id:

- **Several RFQs at once.** Threads are independent, so two (or more) runs can be
  in flight simultaneously. There is no global "one session per socket" state to
  clobber.
- **Stateless HTTP.** Message, action, stop and stream are ordinary
  cookie-authenticated POST/GET requests. A page reload or a second tab neither
  breaks nor hijacks anything.
- **Replay on reconnect.** Each run writes its events to a per-thread queue, so a
  client that connects late (or reconnects after a reload) still receives
  everything from the start of the run.

## Layout

| Concern | Where |
|---|---|
| HTTP routes (threads, messages, upload, stream, stop) | `includes/dashboard/routes/chat_ui.py` |
| Transport-neutral interface | `includes/chat/context.py` (`ChatContext`, `ActionSpec`, `MessageHandle`) |
| SSE implementation of that interface | `includes/chat/context_sse.py` |
| One agent turn | `includes/chat/runner.py` (`run_turn`) |
| Transcript storage | `includes/chat/transcript.py` |
| Pure streaming helpers | `includes/chat/streaming_logic.py` |
| Action registry + dispatcher | `includes/chat/actions.py`, `includes/chat/rfq_actions.py` |
| Dashboard → thread dispatch | `includes/agent_bridge.py` |
| Templates | `templates/chat_ui/{index,thread,embed}.html` |
| Dashboard shell integration | `templates/base.html` (`#chat-ui-embed` + the DOM bridge) |

## How a turn runs

```
POST /chat-ui/threads/{id}/messages
      │
      ▼
_run_task()  ─ build SseChatContext (per-thread asyncio.Queue)
      │       └─ setup_globals(), resolve the thread's agent
      ▼
run_turn(text, ctx)   ← same entry point for chat and dashboard actions
      │
      ├─ every ctx.say() persists a step (transcript) AND queues an SSE event
      ▼
GET /chat-ui/threads/{id}/stream   ← SSE, drains the queue
```

`run_turn` is the single way an agent turn starts. Dashboard buttons reach it via
`dispatch_action_to_thread`, which registers the run in `_active_runs` and returns
immediately — the client then opens the stream.

## Two ways the UI is presented

`GET /chat-ui` and `/chat-ui/threads/{id}` render standalone pages.
`GET /chat-ui/embed` renders the same components for the dashboard's side panel,
where it lives inside the page rather than in an iframe. The dashboard and embed
talk over DOM `CustomEvent`s on `document`, not `postMessage`:

| Event | Direction | Meaning |
|---|---|---|
| `chat-ui:context` | dashboard → embed | which RFQ/view this tab is showing (also POSTed to `/api/dashboard-context`) |
| `chat-ui:navigate` | dashboard → embed | open a thread, or a fresh one |
| `chat-ui:action-started` | dashboard → embed | a run was registered; attach the stream |
| `chat-ui:action-failed` | dashboard → embed | the dispatch was rejected; surface it |
| `chat-ui:thread` | embed → dashboard | a thread was created or selected |
| `chat-ui:ready` | embed → dashboard | the embed finished loading |
| `dashboard:<command>` | embed → dashboard | a run asked for something (`dashboard_refresh`, `agent_navigate`, `agent_working`, `agent_done`) |

Every `dashboard:` event carries `_source_thread`, so the dashboard only refreshes
the page that owns the run it heard from.

## Threads

A thread id **is** the LangGraph `thread_id`, so the transcript, the checkpointer
and any RFQ binding all refer to the same value.

| Table | Purpose |
|---|---|
| `threads`, `steps`, `elements` | conversations, messages, attachments |
| `users` | one row per email |
| `rfq_threads` | RFQ ↔ thread binding (per user) |
| `chat_ui_current_threads` | the user's non-RFQ "home" thread |

An RFQ's thread is resolved (and created if missing) by
`_lookup_rfq_thread_id` in `includes/dashboard/routes/api.py`. That is also the
self-heal path used by the dashboard bridge, which is why a button click on an
RFQ that has never been chatted on works instead of failing.

## Actions

An action is a named handler plus a payload, declared with `ActionSpec`
(`includes/chat/context.py`) and rendered as a button in a message. On click the
client POSTs to `/chat-ui/threads/{id}/action`, which resolves the handler through
`RFQ_ACTIONS` or the `includes/chat/actions.py` registry and runs it inside the
thread's SSE context.

Handlers never touch a transport. They receive `(payload, ctx)` and use `ctx` to
say something, show an image, notify the dashboard, or check whether the user
asked to stop. That is what makes the same handler usable from a chat button, a
dashboard button, and an automated trigger.

## Stopping work

Cancellation is cooperative and **per thread**:

- `POST /chat-ui/threads/{id}/stop` — used by the chat panel's own stop button.
- `POST /api/stop-agent` with `{"thread_id": ...}` — used by the dashboard's
  "working" pill. A thread id is *required*; there is deliberately no stop-all,
  so stopping one run cannot cancel someone else's.

A stop sets a per-run event that handlers poll at safe points (and cancels the
tracked task). The agent's classify step, for example, checks it before starting
the expensive grounded web search so a stop does not spend search quota.

## Attachments

Uploads go to `POST /chat-ui/upload`, which writes to `DATA_DIR/attachments` and
inserts a pending `elements` row (served from the `/files` mount). Sending a
message attaches the element to its step. Deleting an element is only allowed
while it is still pending — an already-sent attachment is not removable.

## Tests

- `tests/test_chat_ui_routes.py` — routes, thread CRUD, uploads, active runs
- `tests/chat/test_sse_context.py` — the SSE context and its event shapes
- `tests/chat/test_streaming_logic.py` — pure streaming helpers
- `tests/chat/test_context_var.py` — `ChatContext` propagation
- `tests/chat/test_run_lock.py` — one run per thread
- `tests/test_bridge_dispatch_sse.py` — action dispatch into a thread
