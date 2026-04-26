# Agent Bridge: Dashboard ↔ Chainlit Communication

The **Agent Bridge** is the framework for bidirectional communication between
the FastAPI dashboard and the Chainlit agent running inside its iframe.

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  FastAPI Dashboard (parent window)              │
│                                                 │
│  ┌───────────────┐    ┌──────────────────────┐  │
│  │ #main-content │    │ #agent-iframe         │  │
│  │  (HTMX views) │    │  src="/chat"          │  │
│  │               │    │  ┌──────────────────┐ │  │
│  │  rfq_detail   │    │  │ Chainlit React   │ │  │
│  │  supplier_det │    │  │ + embedded.js     │ │  │
│  │  product_det  │    │  │ + custom elements │ │  │
│  │               │    │  └──────────────────┘ │  │
│  └───────┬───────┘    └──────────┬───────────┘  │
│          │                       │               │
│          │    /api/agent-bridge  │  postMessage   │
│          └───────────┬───────────┘               │
│                      │                           │
│              ┌───────▼───────┐                   │
│              │  FastAPI      │                   │
│              │  Server       │                   │
│              └───────────────┘                   │
└─────────────────────────────────────────────────┘
```

## Dashboard → Agent (invoking actions)

The dashboard invokes Chainlit action callbacks via a direct HTTP call to the
server.  This avoids the complexity of cross-frame postMessage and socket.io
session discovery.

### Flow

1. Dashboard JS calls `fetch('/api/agent-bridge', { body: { action: { name, payload } } })`
2. Server reads the `X-Chainlit-Session-id` cookie (set by Chainlit's frontend)
3. `includes/agent_bridge.py` looks up the Chainlit `WebsocketSession`
4. Initialises the Chainlit context (`init_ws_context`) so `cl.user_session`,
   `cl.Message`, etc. work normally
5. Dispatches the registered `@cl.action_callback` handler

### Dashboard-side usage

```js
// In any Alpine.js component or template script:
fetch('/api/agent-bridge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
        action: {
            name: 'rfq_find_suppliers',
            payload: { rfq_id: 'RFQ-2026-0001', line: 1, ... }
        }
    }),
});
```

### Server-side handler

Register actions with `@cl.action_callback("action_name")` in `app.py` as
usual.  The bridge dispatches them identically to how Chainlit's own UI does.

### Adding new actions

1. Add `@cl.action_callback("my_action")` in `app.py`
2. Call it from the dashboard via `fetch('/api/agent-bridge', ...)`
3. No changes needed in `agent_bridge.py` — it dispatches by name automatically

## Agent → Dashboard (navigation and data refresh)

Agent-side code can push commands to the dashboard from Python.  This uses
Chainlit's built-in `cl.send_window_message()` which emits a socket.io event
to the iframe; Chainlit's frontend automatically forwards it to the parent
frame via `window.parent.postMessage()`.

### Server-side helper

```python
from includes.agent_bridge import notify_dashboard

# Refresh whatever view the dashboard is currently showing
await notify_dashboard("dashboard_refresh")

# Navigate the dashboard to a specific page
await notify_dashboard("agent_navigate", {"url": "/rfqs/RFQ-2026-0001"})
```

`notify_dashboard()` is safe to call outside a Chainlit context (e.g. in
tests) — it silently skips if no session is active.

**Auto-refresh on RFQ updates**: `_notify_rfq_updated()` in `quote_tools.py`
calls `notify_dashboard("dashboard_refresh")` automatically, so any tool that
modifies RFQ data also refreshes the dashboard.

### JS-side helpers (inside the iframe)

These functions are defined in `public/embedded.js` and can be called from
custom elements or injected scripts:

#### Navigate to a dashboard page

```js
window.navigateDashboard('/suppliers/42');
```

Sends `postMessage({type: 'agent_navigate', payload: {url: '/suppliers/42'}})`.
The parent's `base.html` listener triggers an HTMX fetch to update the view.

#### Refresh the current dashboard view

```js
window.refreshDashboard();
```

Sends `postMessage({type: 'dashboard_refresh'})`.  The parent re-fetches the
current page's partial via HTMX.

### How it works under the hood

```
Python (agent_bridge.notify_dashboard)
  → cl.send_window_message({type: "dashboard_refresh"})
    → socket.io "window_message" event
      → Chainlit frontend: window.parent.postMessage(data, "*")
        → base.html message listener: htmx.ajax('GET', '/partial' + path, ...)
```

### Link interception

Markdown links to `/suppliers/*`, `/products/*`, and `/rfqs/*` rendered inside
the Chainlit chat are automatically intercepted by `embedded.js`.  Clicking
them navigates the parent dashboard instead of navigating inside the iframe.

## Key Files

| File | Role |
|------|------|
| `includes/agent_bridge.py` | Server-side bridge: session lookup, action dispatch, `notify_dashboard()` |
| `includes/tools/quote_tools.py` | `_notify_rfq_updated()` calls `notify_dashboard("dashboard_refresh")` |
| `public/embedded.js` | Iframe-side: theme sync, context push, navigation helpers |
| `public/stylesheet.css` | Hides redundant Chainlit header elements via CSS |
| `templates/base.html` | Parent-side: message listener for navigate/refresh, Alpine `rfqDetail()` |
| `app.py` | Action callbacks (`@cl.action_callback`) |

## Cookie-Based Session Resolution

The Chainlit React frontend calls `POST /set-session-cookie` on connect,
storing the websocket session ID in an `httpOnly` cookie named
`X-Chainlit-Session-id` with `path=/`.  Since the dashboard and Chainlit
share the same origin, this cookie is automatically included in
`fetch('/api/agent-bridge', ...)` calls from the dashboard.

The bridge reads the cookie server-side and uses
`WebsocketSession.get_by_id()` to find the active Chainlit session, then
`init_ws_context()` to set up the execution context.

## Security

- The `/api/agent-bridge` endpoint checks dashboard authentication
  (`get_current_user`) before processing any action.
- The Chainlit session cookie is `httpOnly` (not accessible to JS).
- Action callbacks run in the authenticated user's Chainlit session context.
- postMessage listeners in `base.html` check `event.origin` matches
  `window.location.origin`.
