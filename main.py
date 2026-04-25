"""
FastAPI wrapper for EagleAgent.

Provides Google OAuth login, session management, and mounts Chainlit at /chat.
Dashboard UI will be added in Phase 2.
"""

import os
import logging
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders

from config import config

load_dotenv()

logger = logging.getLogger(__name__)

# Avatar cache directory
AVATAR_CACHE_DIR = Path(config.DATA_DIR) / "avatar_cache"
AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Google OAuth via fastapi-sso
# ---------------------------------------------------------------------------
from fastapi_sso.sso.google import GoogleSSO

google_sso = GoogleSSO(
    client_id=os.environ["OAUTH_GOOGLE_CLIENT_ID"],
    client_secret=os.environ["OAUTH_GOOGLE_CLIENT_SECRET"],
    redirect_uri=None,  # Set dynamically per-request
    allow_insecure_http=config.DEBUG,
)


# ---------------------------------------------------------------------------
# Lifespan — nothing async to initialise at the FastAPI level yet
# (Chainlit's app.py handles its own async setup via setup_globals)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting up")
    yield
    logger.info("FastAPI shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="EagleAgent", lifespan=lifespan)

# Templates
templates = Jinja2Templates(directory="templates")

# Serve Chainlit's /public directory at the root so avatar/image references
# (e.g. /public/avatars/EagleAgent.png) resolve correctly even though
# Chainlit itself is mounted at /chat.
app.mount("/public", StaticFiles(directory="public"), name="public")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> dict | None:
    """Return the user dict from the session, or None."""
    return request.session.get("user")


def require_user(request: Request) -> dict:
    """Dependency that redirects to login if not authenticated."""
    user = get_current_user(request)
    if not user:
        raise _redirect_to_login()
    return user


def _redirect_to_login():
    from fastapi import HTTPException
    from fastapi.responses import RedirectResponse
    # Use a 303 See Other so the browser does a GET on the login page
    raise HTTPException(status_code=303, headers={"Location": "/login"})


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.get("/login")
async def login_page(request: Request):
    """Show login page (or redirect straight to Google)."""
    error = request.query_params.get("error")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
    })


@app.get("/auth/google")
async def google_login(request: Request):
    """Redirect to Google OAuth consent screen."""
    # Build redirect_uri from the request's base URL
    base = str(request.base_url).rstrip("/")
    google_sso.redirect_uri = f"{base}/auth/google/callback"
    async with google_sso:
        return await google_sso.get_login_redirect()


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    """Handle the Google OAuth callback."""
    base = str(request.base_url).rstrip("/")
    google_sso.redirect_uri = f"{base}/auth/google/callback"
    async with google_sso:
        user = await google_sso.verify_and_process(request)

    if not user:
        return RedirectResponse("/login?error=Authentication+failed")

    # Domain check
    email = user.email or ""
    domain = email.split("@")[-1] if "@" in email else ""
    allowed = [d.strip() for d in config.OAUTH_ALLOWED_DOMAINS.split(",") if d.strip()]
    if allowed and domain not in allowed:
        logger.warning(f"OAuth rejected: domain '{domain}' not in {allowed}")
        return RedirectResponse(
            "/login?error=Your+account+is+not+authorised+to+use+this+application"
        )

    # Store user info in session
    request.session["user"] = {
        "email": email,
        "name": getattr(user, "display_name", "") or "",
        "given_name": getattr(user, "first_name", "") or "",
        "family_name": getattr(user, "last_name", "") or "",
        "picture": str(user.picture) if user.picture else "",
        "hd": domain,
    }
    logger.info(f"User logged in: {email}")

    return RedirectResponse("/")


@app.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/avatar")
async def avatar_proxy(request: Request):
    """Serve the current user's Google avatar from a local cache.

    Google profile picture URLs rate-limit when loaded repeatedly (e.g. on
    every page load from an <img> tag).  This endpoint fetches the image
    once, caches it locally, and serves subsequent requests from disk.
    """
    user = get_current_user(request)
    if not user or not user.get("picture"):
        return Response(status_code=204)

    url = user["picture"]
    # Use a hash of the URL as the cache filename
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_path = AVATAR_CACHE_DIR / f"{cache_key}.jpg"

    if not cache_path.exists():
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, follow_redirects=True, timeout=5)
                resp.raise_for_status()
                cache_path.write_bytes(resp.content)
        except Exception as e:
            logger.warning(f"Failed to fetch avatar: {e}")
            return Response(status_code=204)

    return Response(
        content=cache_path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Header injection middleware — syncs FastAPI session auth into Chainlit
# ---------------------------------------------------------------------------
@app.middleware("http")
async def inject_chainlit_auth(request: Request, call_next):
    """For requests to /chat, inject X-Chainlit-User-* headers from the session.

    Security: Any externally-supplied X-Chainlit-User-* headers are stripped
    first — only this middleware may set them.
    """
    if request.url.path.startswith("/chat"):
        # Build clean headers, stripping any spoofed auth headers
        raw_headers = [
            (k, v) for k, v in request.headers.raw
            if not k.lower().startswith(b"x-chainlit-user-")
        ]

        user = request.session.get("user")
        if user:
            raw_headers.append((b"x-chainlit-user-email", user["email"].encode()))
            raw_headers.append((b"x-chainlit-user-name", user.get("name", "").encode()))
            raw_headers.append((b"x-chainlit-user-given-name", user.get("given_name", "").encode()))
            raw_headers.append((b"x-chainlit-user-family-name", user.get("family_name", "").encode()))
            raw_headers.append((b"x-chainlit-user-picture", user.get("picture", "").encode()))
            raw_headers.append((b"x-chainlit-user-hd", user.get("hd", "").encode()))

        # Replace the request's headers with our sanitised + injected set
        scope = request.scope
        scope["headers"] = raw_headers

    return await call_next(request)


# Session middleware — MUST be added AFTER @app.middleware("http") above so
# that Starlette places it outermost and the session is populated before
# inject_chainlit_auth runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["CHAINLIT_AUTH_SECRET"],
    session_cookie="eagleagent_session",
    max_age=60 * 60 * 24 * 15,  # 15 days, matches Chainlit user_session_timeout
    same_site="lax",
    https_only=not config.DEBUG,
)


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------
from includes.dashboard_routes import router as dashboard_router

app.include_router(dashboard_router)


# ---------------------------------------------------------------------------
# Dashboard context API (called by embedded.js in the Chainlit iframe)
# ---------------------------------------------------------------------------
from includes.dashboard_context import set_context as _set_dashboard_context


@app.post("/api/dashboard-context")
async def update_dashboard_context(request: Request):
    """Store the current dashboard view context for the logged-in user."""
    user = get_current_user(request)
    if not user:
        return Response(status_code=401)
    body = await request.json()
    logger.info(f"Dashboard context updated for {user['email']}: {body}")
    _set_dashboard_context(user["email"], body)
    return Response(status_code=204)


@app.get("/api/dashboard-context")
async def get_dashboard_context(request: Request):
    """Debug: return the stored context for the current user."""
    user = get_current_user(request)
    if not user:
        return Response(status_code=401)
    from includes.dashboard_context import get_context
    ctx = get_context(user["email"])
    from includes.dashboard_context import format_context_for_prompt
    formatted = format_context_for_prompt(user["email"])
    import json
    return Response(
        content=json.dumps({"email": user["email"], "context": ctx, "formatted": formatted}, default=str),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Mount Chainlit at /chat
# ---------------------------------------------------------------------------
from chainlit.utils import mount_chainlit

mount_chainlit(app, target="app.py", path="/chat")
