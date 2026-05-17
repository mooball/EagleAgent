"""Admin routes: user management, system admin, job runner, NetSuite status."""

import logging

from fastapi import Request
from sqlalchemy import text

from . import _helpers
from ._helpers import router, templates, require_admin, _render
from .rfqs import _get_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
_USER_STATS_SQL = text("""
    SELECT
        u.id,
        u.identifier,
        COUNT(DISTINCT t.id)  AS thread_count,
        COUNT(s.id)           AS message_count,
        MAX(s."createdAt")    AS last_active
    FROM users u
    LEFT JOIN threads t ON t."userId" = u.id
    LEFT JOIN steps s   ON s."threadId" = t.id
    GROUP BY u.id, u.identifier
    ORDER BY last_active DESC NULLS LAST
""")


def _humanize_timestamp(iso_str: str | None) -> tuple[str, str]:
    """Convert an ISO timestamp to (human_label, exact_datetime)."""
    if not iso_str:
        return ("—", "")
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo(_helpers.config.TIMEZONE)
        raw = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_local = dt.astimezone(local_tz)
        now = datetime.now(local_tz)
        exact = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        time_fmt = dt_local.strftime("%-I:%M %p")

        # Compare calendar dates, not timedelta, to avoid
        # "Today 7 PM" when it's actually yesterday evening.
        days = (now.date() - dt_local.date()).days

        if days == 0:
            label = f"Today {time_fmt}"
        elif days == 1:
            label = f"Yesterday {time_fmt}"
        elif days < 7:
            label = f"{days} days ago"
        elif days < 14:
            label = "Last week"
        elif days < 30:
            weeks = days // 7
            label = f"{weeks} weeks ago"
        elif days < 365:
            months = days // 30
            label = f"{months} month{'s' if months != 1 else ''} ago"
        else:
            label = dt.strftime("%b %Y")

        return (label, exact)
    except (ValueError, TypeError):
        return (iso_str[:16].replace("T", " ") if len(iso_str) > 16 else iso_str, iso_str)


def _query_users(session):
    rows = session.execute(_USER_STATS_SQL).fetchall()
    users = []
    for row in rows:
        human, exact = _humanize_timestamp(row.last_active)
        users.append({
            "id": row.id,
            "identifier": row.identifier,
            "thread_count": row.thread_count,
            "message_count": row.message_count,
            "last_active": human,
            "last_active_exact": exact,
        })
    return users


async def _query_users_with_roles(session):
    """Query user stats and enrich with role + display name."""
    users = _query_users(session)
    admin_emails = _helpers.config.get_admin_emails()
    store = _get_store()
    for u in users:
        email = u["identifier"]
        u["role"] = "Admin" if email.lower() in admin_emails else "Staff"
        u["display_name"] = None
        if store:
            profile = await store.aget(("users",), email)
            if profile and profile.value:
                u["display_name"] = (
                    profile.value.get("preferred_name")
                    or profile.value.get("full_name")
                    or profile.value.get("first_name")
                )
    return users


@router.get("/users")
async def user_list(request: Request, user: dict = require_admin):
    session = _helpers.get_session()
    try:
        users = await _query_users_with_roles(session)
    finally:
        session.close()

    ctx = {"users": users, "active_nav": "users"}
    return _render(request, "users.html", "partials/user_list.html", ctx, user)


@router.get("/partial/users")
async def partial_user_list(request: Request, user: dict = require_admin):
    session = _helpers.get_session()
    try:
        users = await _query_users_with_roles(session)
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/user_list.html", {
        "user": user,
        "users": users,
    })


# ---------------------------------------------------------------------------
# System Admin
# ---------------------------------------------------------------------------

def _job_to_dict(job) -> dict:
    """Convert a Job dataclass to a template-friendly dict."""
    from datetime import datetime, timezone
    if job.finished_at:
        delta = job.finished_at - job.started_at
        duration = str(delta).split(".")[0]
    elif job.status == "running":
        delta = datetime.now(timezone.utc) - job.started_at
        duration = str(delta).split(".")[0]
    else:
        duration = "—"
    return {
        "id": job.id,
        "script_name": job.script_name,
        "status": job.status,
        "started_at": job.started_at,
        "duration": duration,
        "last_output": "\n".join(list(job.output)[-10:]) if job.output else "",
    }


@router.get("/admin")
async def admin_page(request: Request, user: dict = require_admin):
    from config.scripts import list_scripts
    ctx = {
        "scripts": list_scripts(),
        "active_nav": "admin",
    }
    return _render(request, "admin.html", "partials/admin.html", ctx, user)


@router.get("/partial/admin")
async def partial_admin(request: Request, user: dict = require_admin):
    from config.scripts import list_scripts
    return templates.TemplateResponse(request, "partials/admin.html", {
        "user": user,
        "scripts": list_scripts(),
    })


@router.get("/partial/admin/jobs")
async def partial_admin_jobs(request: Request, user: dict = require_admin):
    from includes.graph import job_runner
    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse(request, "partials/admin_jobs.html", {
        "jobs": jobs,
    })


@router.post("/admin/run-script")
async def admin_run_script(request: Request, user: dict = require_admin):
    from includes.graph import job_runner
    from config.scripts import validate_args

    form = await request.form()
    script_name = form.get("script_name", "")
    raw_args = form.get("args", "").strip()
    args = raw_args.split() if raw_args else []

    try:
        validate_args(script_name, args)
        await job_runner.run_script(script_name, args)
    except ValueError as e:
        logger.warning(f"Admin run-script error: {e}")

    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse(request, "partials/admin_jobs.html", {
        "jobs": jobs,
    })


@router.post("/admin/cancel-job")
async def admin_cancel_job(request: Request, user: dict = require_admin):
    from includes.graph import job_runner

    form = await request.form()
    job_id = form.get("job_id", "")

    try:
        await job_runner.cancel(job_id)
    except ValueError as e:
        logger.warning(f"Admin cancel-job error: {e}")

    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse(request, "partials/admin_jobs.html", {
        "jobs": jobs,
    })


@router.get("/partial/admin/netsuite-status")
async def partial_netsuite_status(request: Request, user: dict = require_admin):
    from includes.netsuite import NetSuiteClient
    client = NetSuiteClient()
    result = client.test_connection()
    return templates.TemplateResponse(request, "partials/admin_netsuite_status.html", {
        "netsuite": result,
    })
