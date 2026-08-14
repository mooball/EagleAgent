"""
FastAPI wrapper for EagleAgent.

Provides Google OAuth login, session management, and mounts Chainlit at /chat.
Dashboard UI will be added in Phase 2.
"""

import os
import logging
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
# Background sync loops
# ---------------------------------------------------------------------------
async def _gmail_sync_loop():
    """Periodically sync Gmail mailboxes in a background thread."""
    await asyncio.sleep(30)  # let app fully start
    while True:
        try:
            await asyncio.to_thread(_run_gmail_sync)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Gmail sync error: {e}")
        await asyncio.sleep(config.GMAIL_SYNC_INTERVAL)


def _run_gmail_sync():
    """Run one sync cycle (called in thread pool)."""
    from includes.dashboard.database import get_session
    from includes.dashboard.models import MailboxScanConfig
    from includes.gmail.matching import build_domain_index
    from scripts.sync_gmail_mailboxes import get_enabled_mailboxes, sync_mailbox

    session = get_session()
    try:
        mailboxes = get_enabled_mailboxes(session)
        if not mailboxes:
            return
        domain_index = build_domain_index(session)
        for email in mailboxes:
            try:
                sync_mailbox(session, email, domain_index)
            except Exception as e:
                logger.warning(f"Gmail sync failed for {email}: {e}")
    finally:
        session.close()


async def _netsuite_sync_loop():
    """Periodically sync NetSuite entity data in a background thread."""
    await asyncio.sleep(45)  # let app fully start (after gmail sync)
    while True:
        try:
            await asyncio.to_thread(_run_netsuite_sync)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"NetSuite sync error: {e}")
        await asyncio.sleep(config.NETSUITE_SYNC_INTERVAL)


def _run_netsuite_sync():
    """Run one NetSuite entity sync cycle (called in thread pool)."""
    from scripts.sync_netsuite_entities import run_netsuite_entity_syncs
    results = run_netsuite_entity_syncs()
    passed = sum(1 for v in results.values() if v)
    failed = len(results) - passed
    if failed:
        logger.warning(f"NetSuite entity sync: {passed}/{len(results)} passed, {failed} failed")
    else:
        logger.info(f"NetSuite entity sync: all {passed} steps passed")


# ---------------------------------------------------------------------------
# Background maintenance loop (checkpoint & attachment pruning)
# ---------------------------------------------------------------------------
async def _maintenance_loop():
    """Periodically prune old checkpoints and orphaned attachments."""
    await asyncio.sleep(120)  # let app fully start
    while True:
        try:
            await asyncio.to_thread(_run_maintenance)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")
        await asyncio.sleep(config.MAINTENANCE_INTERVAL)


def _run_maintenance():
    """Run one maintenance cycle (called in thread pool)."""
    import datetime
    import os
    import shutil
    from includes.dashboard.database import get_session
    from sqlalchemy import text

    retention_days = config.CHECKPOINT_RETENTION_DAYS
    attachment_days = config.ATTACHMENT_RETENTION_DAYS
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retention_days)

    session = get_session()
    try:
        # --- Prune old LangGraph checkpoints ---
        # Find threads with no activity since cutoff (based on Chainlit threads table)
        # Chainlit stores "createdAt" as varchar, so cast to timestamptz for comparison
        stale_threads = session.execute(
            text("""
                SELECT id FROM threads
                WHERE "createdAt"::timestamptz < :cutoff
                AND id NOT IN (
                    SELECT DISTINCT "threadId" FROM steps
                    WHERE "createdAt"::timestamptz >= :cutoff
                )
            """),
            {"cutoff": cutoff},
        ).fetchall()

        pruned_threads = 0
        for (tid,) in stale_threads:
            session.execute(text("DELETE FROM checkpoint_writes WHERE thread_id = :tid"), {"tid": tid})
            session.execute(text("DELETE FROM checkpoint_blobs WHERE thread_id = :tid"), {"tid": tid})
            session.execute(text("DELETE FROM checkpoints WHERE thread_id = :tid"), {"tid": tid})
            pruned_threads += 1

        if pruned_threads:
            session.commit()
            logger.info(f"Maintenance: pruned checkpoints for {pruned_threads} stale threads (>{retention_days}d)")

        # --- Prune orphaned attachments ---
        attachments_dir = os.path.join(config.DATA_DIR, "attachments")
        if os.path.isdir(attachments_dir):
            att_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=attachment_days)
            removed_files = 0
            for root, dirs, files in os.walk(attachments_dir, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        mtime = datetime.datetime.fromtimestamp(
                            os.path.getmtime(fpath), tz=datetime.timezone.utc
                        )
                        if mtime < att_cutoff:
                            os.remove(fpath)
                            removed_files += 1
                    except OSError:
                        pass
                # Remove empty directories (but not the root attachments dir)
                if root != attachments_dir and not os.listdir(root):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass

            if removed_files:
                logger.info(f"Maintenance: removed {removed_files} old attachment files (>{attachment_days}d)")

        # --- Sweep expired email uploads (transient attachment staging) ---
        try:
            from includes.dashboard.email_uploads import sweep_expired
            removed_uploads = sweep_expired(config.EMAIL_UPLOAD_TTL_HOURS)
            if removed_uploads:
                logger.info(
                    f"Maintenance: removed {removed_uploads} expired email uploads "
                    f"(>{config.EMAIL_UPLOAD_TTL_HOURS}h)"
                )
        except Exception as e:
            logger.warning(f"Maintenance: email upload sweep failed: {e}")

    except Exception as e:
        session.rollback()
        logger.error(f"Maintenance cycle failed: {e}")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Lifespan — initialise shared async resources (pg pool, store, agents, etc.)
# so that dashboard routes can access the store before any chat session starts.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting up")
    from includes.graph import setup_globals
    await setup_globals()

    # Start background sync tasks if enabled
    gmail_task = None
    netsuite_task = None

    if config.GMAIL_SYNC_ENABLED:
        gmail_task = asyncio.create_task(_gmail_sync_loop())
        logger.info(f"Gmail sync enabled (every {config.GMAIL_SYNC_INTERVAL}s)")

    if config.NETSUITE_SYNC_ENABLED:
        netsuite_task = asyncio.create_task(_netsuite_sync_loop())
        logger.info(f"NetSuite entity sync enabled (every {config.NETSUITE_SYNC_INTERVAL}s)")

    maintenance_task = None
    if config.MAINTENANCE_ENABLED:
        maintenance_task = asyncio.create_task(_maintenance_loop())
        logger.info(
            f"Maintenance loop enabled (every {config.MAINTENANCE_INTERVAL}s, "
            f"checkpoint retention {config.CHECKPOINT_RETENTION_DAYS}d, "
            f"attachment retention {config.ATTACHMENT_RETENTION_DAYS}d)"
        )

    yield

    # Cancel background tasks on shutdown
    if gmail_task:
        gmail_task.cancel()
    if netsuite_task:
        netsuite_task.cancel()
    if maintenance_task:
        maintenance_task.cancel()
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

# Serve uploaded file attachments at /files so Chainlit UI can display
# images and other uploads when resuming a conversation.
app.mount(
    "/files",
    StaticFiles(directory=os.path.join(config.DATA_DIR, "attachments")),
    name="files",
)


# ---------------------------------------------------------------------------
# Graceful handler for Chainlit session expiry
# ---------------------------------------------------------------------------
@app.exception_handler(ValueError)
async def chainlit_session_handler(request: Request, exc: ValueError):
    """Catch Chainlit 'Session not found' errors when a WebSocket session
    expires between tasks.  Returns a friendly message instead of a 500."""
    if "Session not found" in str(exc):
        return JSONResponse(
            status_code=440,
            content={"detail": "Session expired. Please refresh the page."},
        )
    raise exc


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
    return templates.TemplateResponse(request, "login.html", {
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

    return RedirectResponse("/rfqs")


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
from includes.dashboard.routes import router as dashboard_router

app.include_router(dashboard_router)


# ---------------------------------------------------------------------------
# Gmail Add-on API routes (OIDC-authenticated, domain-restricted)
# ---------------------------------------------------------------------------
from includes.dashboard.routes.addon import router as addon_router

app.include_router(addon_router)


# ---------------------------------------------------------------------------
# Dashboard context API (called by embedded.js in the Chainlit iframe)
# ---------------------------------------------------------------------------
from includes.dashboard.context import set_context as _set_dashboard_context


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


# ---------------------------------------------------------------------------
# Agent bridge (dashboard ↔ Chainlit action dispatch)
# ---------------------------------------------------------------------------
from includes.agent_bridge import handle_bridge_request

app.post("/api/agent-bridge")(handle_bridge_request)


@app.post("/api/stop-agent")
async def stop_agent(request: Request):
    """Stop all running agent tasks for the current session.

    This endpoint bypasses the bridge's per-session lock intentionally —
    the lock is held by the running action, so dispatching stop through
    the bridge would deadlock.
    """
    user = get_current_user(request)
    if not user:
        return Response(status_code=401)

    session_id = request.cookies.get("X-Chainlit-Session-id")
    if not session_id:
        return JSONResponse({"error": "No active session"}, status_code=400)

    from includes.agent_bridge import request_stop
    cancelled = await request_stop(session_id)
    return JSONResponse({"stopped": True, "cancelled_tasks": cancelled})


@app.get("/api/dashboard-context")
async def get_dashboard_context(request: Request):
    """Debug: return the stored context for the current user."""
    user = get_current_user(request)
    if not user:
        return Response(status_code=401)
    from includes.dashboard.context import get_context
    ctx = get_context(user["email"])
    from includes.dashboard.context import format_context_for_prompt
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
