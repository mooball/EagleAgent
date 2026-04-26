
# Dashboard Migration Plan

Migrate EagleAgent from a standalone Chainlit app to a FastAPI-driven dashboard
with Chainlit embedded as the chat interface.

## UI Layout

### Wireframe

```
┌─────────────────────────────────────────────────────────────────────┐
│ [Logo]      RFQs  Quotes  Products  Suppliers  Users    [▼ Tom] [⚙]│
├────────────────────────────────────────────┬────────────────────────┤
│                                            │  Agent Chat Panel      │
│                                            │  ┌──────────────────┐  │
│          Main Content Area                 │  │ messages …       │  │
│                                            │  │                  │  │
│   (tables, forms, detail views,            │  │                  │  │
│    dashboards — context-dependent)         │  │                  │  │
│                                            │  │                  │  │
│                                            │  ├──────────────────┤  │
│                                            │  │ [type a message] │  │
│                                            │  └──────────────────┘  │
│                                            │  [─] [□] [✕]          │
└────────────────────────────────────────────┴────────────────────────┘

Agent panel states:
  ✕ Closed   → panel hidden, floating 💬 icon bottom-right
  □ Panel    → right sidebar (~400px), main content shrinks
  ▣ Expanded → agent fills full viewport below header
```

### Header

- **Always visible**, thin bar (~48-56px).
- **Left:** App logo / name.
- **Centre:** Primary navigation links (RFQs, Quotes, Products, Suppliers,
  Users). Active item highlighted. May collapse to a hamburger on mobile.
- **Right:** User avatar + dropdown (profile, settings, logout).

### Main Content Area

- Fills all horizontal space not occupied by the agent panel.
- Renders the active dashboard view (table, detail, form, etc.).
- Resizes fluidly when the agent panel opens/closes.

### Agent Panel

Three toggle states controlled by Alpine.js:

| State | Layout | Trigger |
|-------|--------|---------|
| **Closed** | Hidden. Floating chat bubble icon (bottom-right, `fixed`). | Click ✕ on panel, or bubble to open |
| **Panel** | Right sidebar, ~400px wide. Main content shrinks. | Click bubble, or [□] button |
| **Expanded** | Full width below header (main content hidden). | Click [▣] button |

The panel renders the Chainlit iframe (`/chat`). The iframe is **not
destroyed** when the panel is closed — only hidden via CSS — so conversation
state is preserved across toggles.

## Agent Context Awareness

The agent must always know what the user is currently viewing in the dashboard
so it can answer questions in context ("tell me about this supplier", "add a
line item", etc.) without the user re-stating what they're looking at.

### Context Object

A JSON object describing the current dashboard view, maintained client-side
and pushed to the agent whenever it changes:

```json
{
  "view": "rfq_detail",
  "entity": "rfq",
  "id": "RFQ-2026-0042",
  "db_id": 42,
  "params": { "tab": "line_items" },
  "url": "/rfqs/42?tab=line_items",
  "breadcrumb": ["RFQs", "RFQ-2026-0042", "Line Items"]
}
```

Possible `view` values (will grow):

| view | entity | Description |
|------|--------|-------------|
| `rfq_list` | `rfq` | RFQ table / pipeline |
| `rfq_detail` | `rfq` | Single RFQ with tabs (overview, line items, suppliers) |
| `quote_list` | `quote` | Quotes table |
| `quote_detail` | `quote` | Single quote |
| `product_list` | `product` | Product catalogue |
| `product_detail` | `product` | Single product |
| `supplier_list` | `supplier` | Supplier directory |
| `supplier_detail` | `supplier` | Single supplier |
| `user_list` | `user` | User admin |
| `dashboard` | — | Home / summary dashboard |

### Context Transport: Dashboard → Agent

Two complementary mechanisms:

#### 1. `postMessage` (primary, real-time)

The dashboard pushes context changes to the Chainlit iframe whenever the user
navigates. A small Alpine.js directive watches route changes:

```js
// dashboard side (Alpine.js)
function pushAgentContext(ctx) {
  const iframe = document.getElementById('agent-iframe');
  if (iframe?.contentWindow) {
    iframe.contentWindow.postMessage(
      { type: 'dashboard_context', payload: ctx },
      window.location.origin          // same-origin only
    );
  }
}
```

On the Chainlit side, a custom script (`public/context-bridge.js`) listens
and stores the context so it can be injected into agent prompts:

```js
// Chainlit custom script
window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin) return;  // same-origin guard
  if (event.data?.type === 'dashboard_context') {
    // Store for the agent to read via a Chainlit action or system prompt
    window.__dashboardContext = event.data.payload;
  }
});
```

#### 2. URL query params (fallback / deep links)

When the agent panel is first opened, or the user navigates to a direct URL
that includes the chat, context is passed as query params:

```
/chat?view=rfq_detail&entity=rfq&id=RFQ-2026-0042&db_id=42
```

Chainlit reads these on load and seeds the initial context.

### Context Transport: Agent → Backend

The stored client-side context must reach the LLM. Options:

1. **System message injection** — On each user message, Chainlit's
   `@cl.on_message` reads the current context (passed from the JS bridge via
   a Chainlit action or custom endpoint) and prepends it as a system message:
   ```
   [Dashboard Context] User is viewing: RFQ-2026-0042, tab: line_items
   ```
2. **Custom HTTP header** — The Chainlit iframe's fetch calls could include an
   `X-Dashboard-Context` header (set via service worker or custom fetch
   wrapper) that the backend reads on each request.
3. **Chainlit user session metadata** — Store context in
   `cl.user_session.set("dashboard_context", ctx)` via a custom action
   triggered by `postMessage`.

Option 3 (Chainlit user session) is the cleanest: no header hacking, context
persists for the session, and the agent can read it naturally in tool calls
and prompt construction.

### How the Agent Uses Context

- **Prompt injection:** The agent's system prompt includes the current context
  so the LLM always knows what the user is looking at.
- **Tool routing:** If the user says "add a supplier to this", the agent
  resolves "this" to the RFQ/quote from context rather than asking.
- **Proactive suggestions:** When context changes (new RFQ opened), the agent
  can optionally offer relevant actions ("I see you're looking at RFQ-2026-0042.
  Want me to find suppliers for the missing line items?").

### Agent → Dashboard Navigation

The reverse of context pushing: the agent can **command the dashboard to
navigate** to a specific view. This is how "show RFQ 0004" works once custom
elements are deprecated.

#### Navigation command (postMessage, agent → dashboard)

```js
// Chainlit custom script (inside iframe) — called by navigate_dashboard tool
function navigateDashboard(route) {
  window.parent.postMessage(
    { type: 'agent_navigate', payload: { url: route } },
    window.location.origin
  );
}
```

```js
// Dashboard side (Alpine.js) — listens for navigation commands
window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin) return;
  if (event.data?.type === 'agent_navigate') {
    const url = event.data.payload.url;
    // HTMX-driven navigation: swap main content via AJAX
    htmx.ajax('GET', url, { target: '#main-content', swap: 'innerHTML' });
    history.pushState({}, '', url);
  }
});
```

#### `navigate_dashboard` tool

An LLM-callable tool available to the agent:

```python
@tool
async def navigate_dashboard(entity: str, id: str | None = None,
                              tab: str | None = None) -> str:
    """Navigate the dashboard to show an entity.

    Args:
        entity: One of 'rfq', 'quote', 'product', 'supplier', 'user'.
        id: Optional entity identifier (e.g. 'RFQ-2026-0042' or DB id).
        tab: Optional tab within the detail view.
    """
    route = _build_route(entity, id, tab)
    # Emit navigation via Chainlit custom action → JS bridge
    await cl.Message(
        content=f"Opening {entity}{' ' + id if id else ''} in the dashboard…",
        actions=[cl.Action(
            name="navigate",
            payload={"url": route},
            forId="agent-iframe",
        )],
    ).send()
    return f"Navigated to {route}"
```

#### Behaviour: "show" vs "tell me about"

| User intent | Agent action |
|-------------|-------------|
| "show RFQ 0004" / "open RFQ 0004" | Call `navigate_dashboard(entity='rfq', id='0004')` → dashboard navigates |
| "tell me about RFQ 0004" / "summarise RFQ 0004" | Fetch data, return text summary in chat (no navigation) |
| "show my RFQs" / "open RFQ list" | Call `navigate_dashboard(entity='rfq')` → dashboard shows RFQ list |
| "how many open RFQs do I have?" | Query DB, answer in chat |

## Architecture Overview

| Layer | Technology | Mount Point |
|-------|-----------|-------------|
| Backend / Auth | FastAPI + `fastapi-sso` (Google OAuth) | — |
| Dashboard UI | HTMX + Jinja2 + Tailwind CSS + Alpine.js | `/` |
| Chat UI | Chainlit (mounted as sub-app) | `/chat` |
| Database | PostgreSQL via shared SQLAlchemy engine | — |

## Dashboard Stack

- **HTMX** — server-driven partial page updates via HTML attributes
- **Jinja2** — server-side HTML templates (FastAPI native support)
- **Tailwind CSS** — utility-first styling (already used in Chainlit custom elements)
- **Alpine.js** — lightweight client-side reactivity for toggles, dropdowns, modals
- **Chart.js or ApexCharts** — for any chart/graph widgets (loaded as small JS islands)

## Migration Phases

### Phase 1: FastAPI Wrapper + Chainlit Mount

Wrap the existing Chainlit app under FastAPI. Validate that auth flows work
end-to-end before adding any dashboard UI.

1. Create `main.py` with FastAPI app, Google OAuth via `fastapi-sso`, and
   session middleware.
2. Mount Chainlit at `/chat` using `mount_chainlit`.
3. Configure Chainlit for header-based auth (`auth_callback="header"`).
4. Implement the header injection middleware (see below) and the header
   stripping security guard.
5. Write user profile to the database on OAuth login so Chainlit can retrieve
   metadata (email, name, picture, admin role) from the shared DB — no need
   to pass everything through headers.
6. **Deprecate and remove** the existing Chainlit-native OAuth code:
   - Remove `@cl.oauth_callback` handler from `app.py`
   - Remove `OAUTH_GOOGLE_CLIENT_ID` / `OAUTH_GOOGLE_CLIENT_SECRET` from
     Chainlit's `.env` / config (move to FastAPI config)
   - Keep `ADMIN_EMAILS` — used by both FastAPI middleware and Chainlit
     profile logic
7. Update `Dockerfile`, `start.sh`, `run.sh` — entry point changes from
   `chainlit run app.py` to `uvicorn main:app`.
8. Test: login → redirected to `/chat` → agent works, user identity correct.

### Phase 2: Dashboard UI

Add the HTMX + Jinja2 dashboard alongside the chat.

1. Set up Jinja2 templates with Tailwind CSS and Alpine.js.
2. Create base layout template with navigation, sidebar, and chat toggle.
3. Build initial dashboard views (see Data / Views section below).
4. Ensure dashboard and agent share the same SQLAlchemy engine so data
   updates from the agent appear in the dashboard in real-time.

### Phase 3: Chat Toggle + Context Bridge

1. Implement the three-state agent panel (closed / panel / expanded) using
   Alpine.js — see UI Layout section above.
2. Build the `postMessage` context bridge: dashboard pushes context on every
   route change, Chainlit custom script receives and stores it in
   `cl.user_session`.
3. Update the agent's system prompt construction to include dashboard context
   so the LLM always knows what the user is viewing.
4. Add cross-frame communication for dashboard → agent actions (e.g. clicking
   an RFQ row tells the agent to load it).
5. Polish responsive layout, dark mode, loading states.

### Phase 4: Agent-Driven Navigation + Custom Element Deprecation

Once the dashboard views are stable, migrate display responsibilities from
Chainlit custom elements to the dashboard. The agent stops rendering rich
UI in the chat and instead **navigates the user** to the appropriate
dashboard view.

1. Implement the `navigate` command: the agent sends a `postMessage` from
   the Chainlit iframe to the parent dashboard, which performs the route
   change (see Agent → Dashboard Navigation below).
2. Create a `navigate_dashboard` tool that the LLM can call to trigger
   navigation. When invoked, it emits the navigation command and returns a
   brief confirmation message in the chat (e.g. "Opening RFQ-2026-0042…").
3. **Deprecate the RFQ Custom Element** — remove the Chainlit custom element
   that renders the RFQ detail/list inline in the chat. Replace with a call
   to `navigate_dashboard`.
4. **Deprecate the RFQ List element** — "show my RFQs" navigates to the
   RFQ list view in the dashboard instead of rendering a list in chat.
5. Update agent prompts: when the user asks to "show" or "open" an entity,
   the agent should prefer navigation over inline rendering.
6. Retain the ability to **summarise** in chat — "tell me about RFQ 0004"
   still returns a text summary in chat, but "show me RFQ 0004" navigates.
7. Gradually extend to other entities (quotes, suppliers, products) as their
   dashboard views mature.

## Data / Views

Initial dashboard views (will expand over time):

- **RFQs / Enquiries** — list, detail, status pipeline
- **Quotes** — linked to RFQs, supplier responses
- **Products** — catalogue with part numbers, brands, embeddings status
- **Suppliers** — directory with contacts, brands, purchase history
- **Users** — admin view of user profiles and roles

## Key Implementation Details

### Header Injection Middleware (`main.py`)

Ensures that when the user is logged into the Dashboard, they are automatically
authenticated in the Chat iframe without a second Google OAuth prompt.

```python
@app.middleware("http")
async def sync_auth_to_chainlit(request: Request, call_next):
    if request.url.path.startswith("/chat"):
        # SECURITY: Strip any externally-supplied auth header to prevent
        # spoofing. Only the middleware may set this header.
        headers = [
            (k, v) for k, v in request.headers.raw
            if k != b"x-chainlit-remote-user"
        ]
        user = request.session.get("user")
        if user:
            headers.append(
                (b"x-chainlit-remote-user", user["email"].encode())
            )
        request._headers = Headers(raw=headers)
    return await call_next(request)
```

### Security: Header Auth

The `X-Chainlit-Remote-User` header is trusted by Chainlit to identify the
logged-in user. This creates a spoofing risk if external clients can set the
header directly.

**Mitigations (all required):**

1. **Strip incoming headers** — the middleware must remove any pre-existing
   `X-Chainlit-Remote-User` header before injecting the authenticated value
   (shown in the code above).
2. **Network-level protection** — if running behind a reverse proxy (e.g.
   Railway, nginx), configure it to strip the header from external requests.
3. **Chainlit must not be directly accessible** — only expose the FastAPI app
   externally; Chainlit at `/chat` should only be reachable through the
   FastAPI mount (which runs the middleware first).

### User Profile on Auth

On Google OAuth login, FastAPI writes the user profile to the shared PostgreSQL
database (the same `AsyncPostgresStore` used by the agent). This means:

- Chainlit's header-auth callback only receives the email from the header.
- Chainlit looks up full profile metadata (name, picture, admin flag) from the
  DB using the email as key — same `_ensure_user_profile()` pattern used today.
- No need to encode rich metadata into headers.
- Admin checks (`ADMIN_EMAILS`) continue to work for chat profile visibility.

### Chat Toggle (Alpine.js)

A three-state agent panel: closed (bubble), panel (sidebar), expanded (full).

```html
<div x-data="{ agentState: 'panel' }"
     x-effect="pushAgentContext(currentContext)"
     class="relative h-screen">

  <!-- Main content: hidden when agent is expanded -->
  <main :class="agentState === 'expanded' ? 'hidden' :
                agentState === 'panel'    ? 'mr-[400px]' : ''"
        class="transition-all duration-300">
    <!-- dashboard view rendered here -->
  </main>

  <!-- Agent panel -->
  <template x-if="agentState !== 'closed'">
    <aside :class="agentState === 'expanded'
                     ? 'fixed inset-x-0 top-14 bottom-0 z-50'
                     : 'fixed top-14 right-0 bottom-0 w-[400px] z-40'"
           class="transition-all duration-300 border-l bg-white shadow-xl">
      <div class="flex items-center justify-between px-2 py-1 border-b">
        <span class="text-sm font-medium">Agent</span>
        <div class="flex gap-1">
          <button @click="agentState = 'panel'"  title="Sidebar">□</button>
          <button @click="agentState = 'expanded'" title="Expand">▣</button>
          <button @click="agentState = 'closed'" title="Close">✕</button>
        </div>
      </div>
      <iframe id="agent-iframe" src="/chat" class="w-full h-full border-0"></iframe>
    </aside>
  </template>

  <!-- Floating bubble (when closed) -->
  <template x-if="agentState === 'closed'">
    <button @click="agentState = 'panel'"
            class="fixed bottom-4 right-4 z-40 w-14 h-14 rounded-full
                   bg-blue-600 text-white shadow-lg flex items-center
                   justify-center text-2xl hover:bg-blue-700">
      💬
    </button>
  </template>
</div>
```

### iframe Considerations

The chat runs in an iframe, isolated from the dashboard DOM. This means:

- No shared client-side state between dashboard and chat.
- To communicate (e.g. "load this RFQ"), use `window.postMessage()` from the
  dashboard and listen in a Chainlit custom script, or pass context via URL
  query params (`/chat?rfq=RFQ-2026-0001`).
- Cookie/session sharing works automatically (same origin).

## Deployment Changes

| Item | Before | After |
|------|--------|-------|
| Entry point | `chainlit run app.py` | `uvicorn main:app` |
| Auth | Chainlit OAuth (`@cl.oauth_callback`) | FastAPI `fastapi-sso` + header injection |
| Port config | `--port $PORT` on chainlit CLI | `--port $PORT` on uvicorn CLI |
| Dockerfile CMD | `chainlit run app.py --host 0.0.0.0 --port $PORT` | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Static files | Chainlit serves `/public` | FastAPI mounts `/static` + Chainlit serves `/chat/public` |

## New Dependencies

```
fastapi
fastapi-sso
uvicorn
jinja2
python-multipart        # form handling
itsdangerous            # session signing
```

Tailwind CSS, HTMX, and Alpine.js are loaded via CDN (no Node.js build step
required) or optionally via the Tailwind standalone CLI for production builds.
