# Agent Bridge: Dashboard → Chat

The **Agent Bridge** connects a dashboard button click to the chat thread that
owns the RFQ. It is deliberately one-directional in plumbing: dashboard → thread,
over an ordinary authenticated POST.

The other direction needs no bridge at all — an agent asks the dashboard for
something by queueing an event on its own SSE stream, which the embed forwards to
the shell as a DOM event. See [Chat UI](./CHAT_UI.md).

> **History.** This used to be a bidirectional bridge over Chainlit's socket.io:
> the dashboard read the `X-Chainlit-Session-id` cookie, looked up the
> `WebsocketSession`, and called `init_ws_context()` before dispatching.
> `notify_dashboard()` went out through `cl.send_window_message()` and an iframe
> `postMessage` hop in `public/embedded.js`. All of that is gone.

## Why a bridge is still needed

A dashboard button is not a chat message. Clicking it has to run a handler
*outside* a graph turn, inside the right conversation, with the panel showing the
result. That is what `handle_bridge_request` does.

## Dashboard → thread

```
dashboard button (_sendAction)
  │  POST /api/agent-bridge   {action: {name, payload}}
  │     payload carries rfq_id and, when known, _thread_id
  ▼
agent_bridge.handle_bridge_request
  │  1. authenticate the dashboard session
  │  2. resolve the target thread:
  │       _thread_id from the payload, else
  │       _lookup_rfq_thread_id(rfq_id, user)  ← finds, repairs, or CREATES + binds
  │  3. dispatch_action_to_thread(user, thread_id, name, payload)
  ▼
chat_ui._execute_action → handler(payload, ctx) → results on the thread's SSE stream
```

The response includes the `thread_id` the run went to, and the client uses it to
open that conversation if the server had to create one.

### Why the server resolves the thread

The client says which RFQ the click belongs to; the **server** decides which
conversation that is. This matters because:

- a brand-new RFQ has no thread yet;
- a click can land before the client's own binding round-trip finishes;
- a bound thread may since have been deleted, leaving a stale binding.

In all three cases the server finds, repairs or creates the thread instead of
failing — and, critically, never falls back to "whatever conversation happens to
be open", which is how an RFQ's action output previously ended up in an unrelated
chat.

### Dashboard-side call

```js
// templates/base.html — the single choke point is rfqDetail()._sendAction()
fetch('/api/agent-bridge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
        action: {
            name: 'rfq_find_suppliers',
            payload: { rfq_id: 'RFQ-2026-0001', line: 1, _thread_id: '...' }
        }
    }),
});
```

`_sendAction()` also fires an optimistic `agent-working` event so the "working"
pill appears immediately, and clears it if the POST is rejected.

### Adding an action

1. Write a `(payload, ctx)` handler and register it in `RFQ_ACTIONS`
   (`includes/chat/rfq_actions.py`), or as `(ctx, payload=...)` in
   `includes/chat/actions.py`.
2. Call it by name from the dashboard via `/api/agent-bridge`.

There is nothing to register in the bridge — it resolves handlers by name at
dispatch time.

## Thread → dashboard

An agent asks for something by queueing an event on its own stream:

```python
await ctx.notify_dashboard("dashboard_refresh")
await ctx.notify_dashboard("agent_navigate", {"url": "/rfqs/RFQ-2026-0001"})
await ctx.notify_dashboard("agent_working", {"label": "Searching suppliers..."})
```

```
ctx.notify_dashboard(cmd)
  → SSE 'dashboard' event on the thread's stream
    → embed.html: document.dispatchEvent('dashboard:' + cmd, {_source_thread})
      → base.html:  _handleDashboardCommand(cmd, payload)
```

Commands in use: `dashboard_refresh`, `agent_navigate`, `agent_working`,
`agent_done`. Every event carries `_source_thread`, so a run on RFQ A can only
refresh the page that belongs to RFQ A.

**Auto-refresh on RFQ updates**: `_notify_rfq_updated()` in `quote_tools.py` calls
`ctx.notify_dashboard("dashboard_refresh")`, so any tool that modifies RFQ data
also refreshes the dashboard.

## Stopping work

Cancellation is cooperative and **per thread** (see [Chat UI](./CHAT_UI.md)):

- `POST /chat-ui/threads/{id}/stop` — the chat panel's own stop button.
- `POST /api/stop-agent` with `{"thread_id": ...}` — the dashboard's "working"
  pill. A thread id is **required**; there is deliberately no stop-all, so
  stopping one run cannot cancel someone else's.

## Key files

| File | Role |
|------|------|
| `includes/agent_bridge.py` | `handle_bridge_request` (dashboard → thread) + cooperative cancellation registry |
| `includes/dashboard/routes/chat_ui.py` | `dispatch_action_to_thread`, `_execute_action`, `_active_runs` |
| `includes/dashboard/routes/api.py` | `_lookup_rfq_thread_id` — find/repair/create the RFQ's thread |
| `templates/base.html` | `_sendAction()` (dispatch), `_handleDashboardCommand()` (inbound), `stopAgent()` |
| `templates/chat_ui/embed.html` | Drains the SSE stream; re-emits `dashboard` events as DOM events |
| `includes/tools/quote_tools.py` | `_notify_rfq_updated()` → `dashboard_refresh` |

## Security

- `/api/agent-bridge` authenticates the dashboard session before dispatching.
- `/chat-ui/*` requires an authenticated user, and thread ownership is checked on
  every read and write, so you cannot act on someone else's conversation.
- `/api/stop-agent` requires a `thread_id` and will only stop runs on threads you
  own.
