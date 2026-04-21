
# Dashboard Migration Plan

Migrate EagleAgent from a standalone Chainlit app to a FastAPI-driven dashboard
with Chainlit embedded as the chat interface.

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

### Phase 3: Chat Toggle + Polish

1. Implement the expandable chat iframe widget — fixed-position panel that
   expands from a sidebar widget to a full-viewport overlay (Alpine.js state).
2. Add cross-frame communication via `postMessage` if needed (e.g. clicking
   an RFQ row in the dashboard tells the chat to load it).
3. Polish responsive layout, dark mode, loading states.

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

A fixed-position chat panel that can be toggled between compact and full-screen.

```html
<div x-data="{ expanded: false }"
     :class="expanded ? 'fixed inset-0 z-50' : 'fixed bottom-4 right-4 w-[400px] h-[600px] z-40'"
     class="transition-all duration-300 rounded-lg shadow-xl overflow-hidden">
  <button @click="expanded = !expanded"
          class="absolute top-2 right-2 z-10 bg-gray-800 text-white rounded-full p-1">
    <template x-if="expanded"><!-- collapse icon --></template>
    <template x-if="!expanded"><!-- expand icon --></template>
  </button>
  <iframe src="/chat" class="w-full h-full border-0"></iframe>
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
