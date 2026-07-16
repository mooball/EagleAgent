"""Admin routes: user management, system admin, job runner, NetSuite status."""

import logging

from fastapi import Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse
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
        MAX(s."createdAt")    AS last_active,
        COALESCE(r.rfq_count, 0) AS rfq_count
    FROM users u
    LEFT JOIN threads t ON t."userId" = u.id
    LEFT JOIN steps s   ON s."threadId" = t.id
    LEFT JOIN (
        SELECT email, COUNT(*) AS rfq_count
        FROM (
            SELECT created_by AS email FROM rfqs
            UNION ALL
            SELECT assigned_to AS email FROM rfqs WHERE assigned_to IS NOT NULL
        ) rfq_users
        GROUP BY email
    ) r ON r.email = u.identifier
    GROUP BY u.id, u.identifier, r.rfq_count
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
        tc = row.thread_count or 0
        mc = row.message_count or 0
        mpt = round(mc / tc, 1) if tc > 0 else 0
        users.append({
            "id": row.id,
            "identifier": row.identifier,
            "thread_count": tc,
            "message_count": mc,
            "msgs_per_thread": mpt,
            "rfq_count": row.rfq_count,
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
async def user_list(request: Request, user: dict = require_admin) -> HTMLResponse:
    session = _helpers.get_session()
    try:
        users = await _query_users_with_roles(session)
    finally:
        session.close()

    ctx = {"users": users, "active_nav": "users"}
    return _render(request, "users.html", "partials/user_list.html", ctx, user)


@router.get("/partial/users")
async def partial_user_list(request: Request, user: dict = require_admin) -> HTMLResponse:
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
async def admin_page(request: Request, user: dict = require_admin) -> HTMLResponse:
    from config.scripts import list_scripts
    ctx = {
        "scripts": list_scripts(),
        "active_nav": "admin",
    }
    return _render(request, "admin.html", "partials/admin.html", ctx, user)


@router.get("/partial/admin")
async def partial_admin(request: Request, user: dict = require_admin) -> HTMLResponse:
    from config.scripts import list_scripts
    return templates.TemplateResponse(request, "partials/admin.html", {
        "user": user,
        "scripts": list_scripts(),
    })


@router.get("/partial/admin/jobs")
async def partial_admin_jobs(request: Request, user: dict = require_admin) -> HTMLResponse:
    from includes.graph import job_runner
    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse(request, "partials/admin_jobs.html", {
        "jobs": jobs,
    })


@router.post("/admin/run-script")
async def admin_run_script(request: Request, user: dict = require_admin) -> HTMLResponse:
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
async def admin_cancel_job(request: Request, user: dict = require_admin) -> HTMLResponse:
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
async def partial_netsuite_status(request: Request, user: dict = require_admin) -> HTMLResponse:
    from includes.netsuite import NetSuiteClient
    client = NetSuiteClient()
    result = client.test_connection()
    return templates.TemplateResponse(request, "partials/admin_netsuite_status.html", {
        "netsuite": result,
    })


# ---------------------------------------------------------------------------
# Supplier Deduplication
# ---------------------------------------------------------------------------

@router.get("/admin/duplicates")
async def admin_duplicates(request: Request, user: dict = require_admin) -> HTMLResponse:
    ctx = {"active_nav": "admin", "duplicates": None, "scanned": False}
    return _render(request, "admin_duplicates.html", "partials/admin_duplicates.html", ctx, user)


@router.get("/partial/admin/duplicates")
async def partial_admin_duplicates(request: Request, user: dict = require_admin) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/admin_duplicates.html", {
        "user": user,
        "active_nav": "admin",
        "duplicates": None,
        "scanned": False,
    })


@router.post("/admin/duplicates/scan")
async def admin_duplicates_scan(request: Request, user: dict = require_admin) -> HTMLResponse:
    import asyncio
    from scripts.find_duplicate_suppliers import scan_duplicates, scan_internal_duplicates

    form = await request.form()
    scan_mode = form.get("scan_mode", "netsuite")

    session = _helpers.get_session()
    try:
        if scan_mode == "internal":
            duplicates = await asyncio.to_thread(scan_internal_duplicates, session)
        else:
            duplicates = await asyncio.to_thread(scan_duplicates, session)
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_admin_dedup_results.html", {
        "user": user,
        "active_nav": "admin",
        "duplicates": duplicates,
        "scanned": True,
        "scan_mode": scan_mode,
    })


@router.post("/admin/duplicates/merge")
async def admin_duplicates_merge(request: Request, user: dict = require_admin) -> HTMLResponse:
    import asyncio
    from starlette.responses import HTMLResponse
    from scripts.find_duplicate_suppliers import merge_supplier

    form = await request.form()
    keep_id = form.get("keep_id", "").strip()
    remove_id = form.get("remove_id", "").strip()
    merge_url = form.get("merge_url") == "1"
    merge_contacts = form.get("merge_contacts") == "1"

    if not keep_id or not remove_id:
        return HTMLResponse('<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">Missing supplier IDs.</div>')

    session = _helpers.get_session()
    try:
        merge_fields = {"url": merge_url, "contacts": merge_contacts}
        result = await asyncio.to_thread(merge_supplier, session, keep_id, remove_id, merge_fields)
        if result["status"] == "ok":
            session.commit()
            msg = f"Merged successfully. Updated {result['updated_rfq_items']} RFQ item(s)."
            return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300 border border-green-200 dark:border-green-800 mb-3">{msg}</div>')
        else:
            session.rollback()
            msg = f"Error: {result['message']}"
            return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">{msg}</div>')
    except Exception as e:
        session.rollback()
        logger.exception("Merge failed")
        return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">Error: {e}</div>')
    finally:
        session.close()


@router.post("/admin/duplicates/dismiss")
async def admin_duplicates_dismiss(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Mark a non-netsuite supplier as 'not a duplicate' by adding a flag."""
    import uuid
    from starlette.responses import HTMLResponse
    from sqlalchemy.orm.attributes import flag_modified
    from includes.dashboard.models import Supplier

    form = await request.form()
    supplier_id = form.get("supplier_id", "").strip()

    if not supplier_id:
        return HTMLResponse("")

    session = _helpers.get_session()
    try:
        sup = session.query(Supplier).filter(Supplier.id == uuid.UUID(supplier_id)).first()
        if sup:
            from datetime import datetime, timezone
            comments = list(sup.comments or [])
            comments.append({
                "author": user.get("identifier", "admin"),
                "comment": "Marked as not-a-duplicate during dedup review.",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            sup.comments = comments
            flag_modified(sup, "comments")
            alt_names = list(sup.alt_names or [])
            if "__dedup_reviewed__" not in alt_names:
                alt_names.append("__dedup_reviewed__")
                sup.alt_names = alt_names
                flag_modified(sup, "alt_names")
            session.commit()
    except Exception as e:
        session.rollback()
        logger.warning(f"Dismiss failed: {e}")
        return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">Dismiss failed: {e}</div>')
    finally:
        session.close()

    return HTMLResponse('<div class="px-4 py-3 rounded-lg text-sm bg-gray-50 text-gray-600 dark:bg-gray-800 dark:text-gray-400 border border-gray-200 dark:border-gray-700 mb-3">Dismissed.</div>')


@router.post("/admin/duplicates/delete")
async def admin_duplicates_delete(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Delete a supplier outright (with RFQ cleanup)."""
    import uuid
    from starlette.responses import HTMLResponse
    from sqlalchemy.orm.attributes import flag_modified
    from includes.dashboard.models import Supplier, RFQItem, SupplierBrand

    form = await request.form()
    supplier_id = form.get("supplier_id", "").strip()

    if not supplier_id:
        return HTMLResponse("")

    session = _helpers.get_session()
    try:
        sup_uuid = uuid.UUID(supplier_id)
        sup = session.query(Supplier).filter(Supplier.id == sup_uuid).first()
        if not sup:
            return HTMLResponse('<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">Supplier not found.</div>')

        sup_name = sup.name

        # Remove from RFQ items suppliers JSONB
        rfq_items = session.query(RFQItem).filter(RFQItem.suppliers.isnot(None)).all()
        updated = 0
        for item in rfq_items:
            suppliers = item.suppliers or []
            new_list = [s for s in suppliers if s.get("supplier_id") != supplier_id]
            if len(new_list) != len(suppliers):
                item.suppliers = new_list
                flag_modified(item, "suppliers")
                updated += 1

        # Remove from RFQ items brand_suppliers JSONB
        brand_items = session.query(RFQItem).filter(RFQItem.brand_suppliers.isnot(None)).all()
        for item in brand_items:
            brand_sups = item.brand_suppliers or []
            new_list = [s for s in brand_sups if s.get("supplier_id") != supplier_id]
            if len(new_list) != len(brand_sups):
                item.brand_suppliers = new_list
                flag_modified(item, "brand_suppliers")

        # Remove supplier_brand links
        session.query(SupplierBrand).filter(SupplierBrand.supplier_id == sup_uuid).delete()

        # Null out email_tracking references to this supplier
        from includes.dashboard.models import EmailTracking, Contact, Transaction
        email_count = (
            session.query(EmailTracking)
            .filter(EmailTracking.supplier_id == sup_uuid)
            .update({"supplier_id": None}, synchronize_session=False)
        )

        # Null out contact references to this supplier
        contact_count = (
            session.query(Contact)
            .filter(Contact.supplier_id == sup_uuid)
            .update({"supplier_id": None}, synchronize_session=False)
        )

        # Block delete if product_suppliers (Transaction) references exist —
        # these have a NOT NULL FK so they can't be NULLed; use Merge instead.
        txn_count = (
            session.query(Transaction)
            .filter(Transaction.supplier_id == sup_uuid)
            .count()
        )
        if txn_count > 0:
            session.rollback()
            return HTMLResponse(
                f'<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">'
                f'Cannot delete &ldquo;{sup_name}&rdquo;: it has {txn_count} transaction record(s). '
                f'Use <strong>Merge</strong> instead to reassign them to the kept supplier.'
                f'</div>'
            )

        # Delete the supplier
        session.delete(sup)
        session.commit()

        msg = f"Deleted &ldquo;{sup_name}&rdquo;."
        if updated:
            msg += f" Removed from {updated} RFQ item(s)."
        if email_count:
            msg += f" Unlinked {email_count} email tracking record(s)."
        if contact_count:
            msg += f" Unlinked {contact_count} contact(s)."
        return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300 border border-green-200 dark:border-green-800 mb-3">{msg}</div>')
    except Exception as e:
        session.rollback()
        logger.exception("Delete supplier failed")
        return HTMLResponse(f'<div class="px-4 py-3 rounded-lg text-sm bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 border border-red-200 dark:border-red-800 mb-3">Delete failed: {e}</div>')
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Email Logs
# ---------------------------------------------------------------------------

_EMAIL_PAGE_SIZE = 50


def _query_email_logs(session, q: str = "", user_filter: str = "", page: int = 1):
    """Query email_tracking with optional filters. Returns (emails, total, has_more, next_page)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    local_tz = ZoneInfo(_helpers.config.TIMEZONE)

    where_clauses = []
    params = {}

    if q:
        where_clauses.append(
            "(et.subject ILIKE :q OR s.name ILIKE :q OR c.companyname ILIKE :q)"
        )
        params["q"] = f"%{q}%"

    if user_filter:
        where_clauses.append(
            "(et.user_email = :uf OR et.sender_email = :uf OR et.recipient_email = :uf)"
        )
        params["uf"] = user_filter

    where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""

    count_row = session.execute(
        text(f"""
            SELECT COUNT(*) FROM email_tracking et
            LEFT JOIN suppliers s ON et.supplier_id = s.id
            LEFT JOIN customers c ON et.customer_id = c.id
            WHERE 1=1 {where_sql}
        """),
        params,
    ).scalar()

    total = count_row or 0
    import math
    total_pages = max(1, math.ceil(total / _EMAIL_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _EMAIL_PAGE_SIZE

    params["limit"] = _EMAIL_PAGE_SIZE
    params["offset"] = offset

    rows = session.execute(
        text(f"""
            SELECT
                et.id,
                et.direction,
                et.email_type,
                et.subject,
                et.recipient_email,
                et.user_email,
                et.sender_email,
                et.rfq_id,
                et.rfq_token,
                et.gmail_thread_id,
                et.gmail_message_id,
                et.gmail_draft_id,
                et.sent_at,
                et.created_at,
                et.match_type,
                et.supplier_pipeline_result,
                s.name AS supplier_name,
                c.companyname AS customer_name
            FROM email_tracking et
            LEFT JOIN suppliers s ON et.supplier_id = s.id
            LEFT JOIN customers c ON et.customer_id = c.id
            WHERE 1=1 {where_sql}
            ORDER BY COALESCE(et.sent_at, et.created_at) DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()

    emails = []
    for row in rows:
        e = dict(row)
        ts = e.get("sent_at") or e.get("created_at")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            dt_local = ts.astimezone(local_tz)
            e["display_time"] = dt_local.strftime("%Y-%m-%d %H:%M")
        elif isinstance(ts, str):
            e["display_time"] = ts[:16].replace("T", " ") if len(ts) >= 16 else ts
        else:
            e["display_time"] = "—"
        emails.append(e)

    return emails, total, page < total_pages, page + 1


def _get_email_user_list(session) -> list[dict]:
    """Get active staff from netsuite_employee_mappings for the user filter."""
    rows = session.execute(
        text("SELECT email, name FROM netsuite_employee_mappings WHERE email IS NOT NULL AND is_active = true ORDER BY name")
    ).fetchall()
    return [{"email": r[0], "name": r[1]} for r in rows]


def _resolve_email_filter(user: dict, user_filter: str) -> tuple[str, bool]:
    """Return (effective_user_filter, can_change_filter) based on user role."""
    if user["role"] == "Admin":
        return user_filter, True
    # Non-admin: always filter to their own email
    return user.get("email", ""), False


@router.get("/emails")
async def email_logs(request: Request, user: dict = Depends(_helpers.require_user),
                     q: str = "", user_filter: str = "", page: int = 1):
    """Email logs page — all staff can view their own; admins see all."""
    effective_filter, can_change_filter = _resolve_email_filter(user, user_filter)
    session = _helpers.get_session()
    try:
        emails, total, has_more, next_page = _query_email_logs(session, q, effective_filter, page)
        email_users = _get_email_user_list(session) if can_change_filter else []
        ctx = {
            "emails": emails,
            "page_title": "Email Logs",
            "email_count": total,
            "q": q,
            "user_filter": effective_filter,
            "email_users": email_users,
            "can_change_filter": can_change_filter,
            "has_more": has_more,
            "next_page": next_page,
        }
        return _render(
            request, "admin_emails.html", "partials/admin_emails.html", ctx, user
        )
    finally:
        session.close()


@router.get("/admin/emails")
async def admin_email_logs_redirect(request: Request, user: dict = Depends(_helpers.require_user)):
    """Redirect old /admin/emails URL to /emails."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/emails", status_code=302)


@router.get("/partial/email-rows")
async def partial_email_rows(request: Request, user: dict = Depends(_helpers.require_user),
                             q: str = "", user_filter: str = "", page: int = 1) -> HTMLResponse:
    """Return just the <tr> rows + sentinel for infinite scroll."""
    effective_filter, _ = _resolve_email_filter(user, user_filter)
    session = _helpers.get_session()
    try:
        emails, total, has_more, next_page = _query_email_logs(session, q, effective_filter, page)
        return templates.TemplateResponse(request, "partials/_email_rows.html", {
            "user": user,
            "emails": emails,
            "q": q,
            "user_filter": effective_filter,
            "has_more": has_more,
            "next_page": next_page,
        })
    finally:
        session.close()


@router.get("/partial/emails")
async def partial_emails(request: Request, user: dict = Depends(_helpers.require_user),
                         q: str = "", user_filter: str = "", page: int = 1) -> HTMLResponse:
    """Return the full email logs partial (for filter form submissions via HTMX)."""
    effective_filter, can_change_filter = _resolve_email_filter(user, user_filter)
    session = _helpers.get_session()
    try:
        emails, total, has_more, next_page = _query_email_logs(session, q, effective_filter, page)
        email_users = _get_email_user_list(session) if can_change_filter else []
        ctx = {
            "user": user,
            "emails": emails,
            "page_title": "Email Logs",
            "email_count": total,
            "q": q,
            "user_filter": effective_filter,
            "email_users": email_users,
            "can_change_filter": can_change_filter,
            "has_more": has_more,
            "next_page": next_page,
        }
        return templates.TemplateResponse(request, "partials/admin_emails.html", ctx)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Mailbox Scan Config
# ---------------------------------------------------------------------------

@router.get("/admin/mailboxes")
async def admin_mailboxes(request: Request, user: dict = require_admin):
    """Admin page for managing Gmail mailbox scanning config."""
    session = _helpers.get_session()
    try:
        from includes.dashboard.models import MailboxScanConfig
        configs = session.query(MailboxScanConfig).order_by(MailboxScanConfig.user_email).all()
        ctx = {"mailboxes": configs, "page_title": "Mailbox Scanning"}
        return _render(
            request, "admin_mailboxes.html", "partials/admin_mailboxes.html", ctx, user
        )
    finally:
        session.close()


@router.post("/admin/mailboxes/add")
async def admin_mailbox_add(request: Request, user: dict = require_admin):
    """Add a mailbox to the scan config."""
    from fastapi.responses import HTMLResponse
    form = await request.form()
    email = form.get("user_email", "").strip().lower()

    if not email or "@" not in email:
        return HTMLResponse('<div class="text-red-600 text-sm">Invalid email address</div>')

    session = _helpers.get_session()
    try:
        from includes.dashboard.models import MailboxScanConfig
        existing = session.query(MailboxScanConfig).filter_by(user_email=email).first()
        if existing:
            return HTMLResponse(f'<div class="text-yellow-600 text-sm">{email} already configured</div>')

        # Auto-disable non-eagle-exports.com domains
        scan_enabled = email.endswith("@eagle-exports.com")
        excluded_reason = None if scan_enabled else "non-eagle-exports domain"

        config_entry = MailboxScanConfig(
            user_email=email,
            scan_enabled=scan_enabled,
            excluded_reason=excluded_reason,
        )
        session.add(config_entry)
        session.commit()

        # Return updated list via redirect
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/admin/mailboxes", status_code=303)
    except Exception as e:
        session.rollback()
        return HTMLResponse(f'<div class="text-red-600 text-sm">Error: {e}</div>')
    finally:
        session.close()


@router.post("/admin/mailboxes/{email:path}/toggle")
async def admin_mailbox_toggle(request: Request, email: str, user: dict = require_admin):
    """Toggle scanning on/off for a mailbox."""
    from fastapi.responses import HTMLResponse
    session = _helpers.get_session()
    try:
        from includes.dashboard.models import MailboxScanConfig
        config_entry = session.query(MailboxScanConfig).filter_by(user_email=email).first()
        if not config_entry:
            return HTMLResponse(f'<div class="text-red-600 text-sm">Mailbox not found</div>')

        config_entry.scan_enabled = not config_entry.scan_enabled
        config_entry.excluded_reason = None if config_entry.scan_enabled else "disabled by admin"
        session.commit()

        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/admin/mailboxes", status_code=303)
    except Exception as e:
        session.rollback()
        return HTMLResponse(f'<div class="text-red-600 text-sm">Error: {e}</div>')
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Email Linking API
# ---------------------------------------------------------------------------

@router.get("/api/admin/search-entities")
async def api_search_entities(request: Request, type: str, q: str, user: dict = Depends(_helpers.require_user)):
    """Search suppliers or customers by name for the email linking UI."""
    if not q or len(q) < 2:
        return JSONResponse({"results": []})

    session = _helpers.get_session()
    try:
        if type == "supplier":
            rows = session.execute(
                text("SELECT id, name FROM suppliers WHERE LOWER(name) LIKE :q ORDER BY name LIMIT 10"),
                {"q": f"%{q.lower()}%"}
            ).mappings().all()
            return JSONResponse({"results": [{"id": str(r["id"]), "name": r["name"]} for r in rows]})
        elif type == "customer":
            rows = session.execute(
                text("SELECT id, companyname FROM customers WHERE LOWER(companyname) LIKE :q AND isinactive = false ORDER BY companyname LIMIT 10"),
                {"q": f"%{q.lower()}%"}
            ).mappings().all()
            return JSONResponse({"results": [{"id": str(r["id"]), "name": r["companyname"]} for r in rows]})
        else:
            return JSONResponse({"results": []})
    finally:
        session.close()


@router.post("/api/admin/link-email")
async def api_link_email(request: Request, user: dict = Depends(_helpers.require_user)):
    """Link an email to an RFQ, customer, or supplier. Optionally save domain for future matching."""
    from includes.dashboard.models import EmailTracking, Supplier, Customer

    body = await request.json()
    email_id = body.get("email_id")
    link_type = body.get("link_type")  # 'rfq', 'customer', 'supplier'
    save_domain = body.get("save_domain", False)

    if not email_id or not link_type:
        return JSONResponse({"status": "error", "message": "Missing email_id or link_type"})

    session = _helpers.get_session()
    try:
        tracking = session.query(EmailTracking).filter(EmailTracking.id == email_id).first()
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"})

        if link_type == "rfq":
            rfq_token = body.get("rfq_token", "").strip()
            if not rfq_token:
                return JSONResponse({"status": "error", "message": "No RFQ token provided"})
            # Update this email and all others in the same thread
            filters = [EmailTracking.id == email_id]
            if tracking.gmail_thread_id:
                filters = [EmailTracking.gmail_thread_id == tracking.gmail_thread_id]
            session.execute(
                text("""
                    UPDATE email_tracking SET rfq_token = :token, match_type = 'manual'
                    WHERE gmail_thread_id = :tid OR id = :eid
                """),
                {"token": rfq_token, "tid": tracking.gmail_thread_id or "", "eid": email_id}
            )
            session.commit()
            # Trigger quote pipeline if now linked to both RFQ + supplier
            if tracking.supplier_id and tracking.direction == "received":
                from includes.tools.supplier_quote_pipeline import trigger_supplier_quote_pipeline
                trigger_supplier_quote_pipeline(email_id, user_id=user.get("email", "manual"))
            return JSONResponse({"status": "ok", "message": f"Linked thread to {rfq_token}"})

        elif link_type == "customer":
            entity_id = body.get("entity_id")
            if not entity_id:
                return JSONResponse({"status": "error", "message": "No customer selected"})
            # Link this email (and thread) to the customer
            session.execute(
                text("""
                    UPDATE email_tracking SET customer_id = :cid, match_type = 'manual'
                    WHERE gmail_thread_id = :tid OR id = :eid
                """),
                {"cid": entity_id, "tid": tracking.gmail_thread_id or "", "eid": email_id}
            )

            # Save domain if requested
            domain_msg = ""
            if save_domain:
                domain_msg = _save_email_domain(session, tracking, "customer", entity_id)

            session.commit()
            customer = session.query(Customer).filter(Customer.id == entity_id).first()
            name = customer.companyname if customer else "customer"
            return JSONResponse({"status": "ok", "message": f"Linked to {name}. {domain_msg}".strip()})

        elif link_type == "supplier":
            entity_id = body.get("entity_id")
            if not entity_id:
                return JSONResponse({"status": "error", "message": "No supplier selected"})
            # Link this email (and thread) to the supplier
            session.execute(
                text("""
                    UPDATE email_tracking SET supplier_id = :sid, match_type = 'manual'
                    WHERE gmail_thread_id = :tid OR id = :eid
                """),
                {"sid": entity_id, "tid": tracking.gmail_thread_id or "", "eid": email_id}
            )

            # Save domain if requested
            domain_msg = ""
            if save_domain:
                domain_msg = _save_email_domain(session, tracking, "supplier", entity_id)

            session.commit()
            supplier = session.query(Supplier).filter(Supplier.id == entity_id).first()
            name = supplier.name if supplier else "supplier"
            # Trigger quote pipeline if now linked to both supplier + RFQ
            rfq_id = tracking.rfq_token or tracking.rfq_id
            if rfq_id and tracking.direction == "received":
                from includes.tools.supplier_quote_pipeline import trigger_supplier_quote_pipeline
                trigger_supplier_quote_pipeline(email_id, user_id=user.get("email", "manual"))
            return JSONResponse({"status": "ok", "message": f"Linked to {name}. {domain_msg}".strip()})

        else:
            return JSONResponse({"status": "error", "message": f"Unknown link type: {link_type}"})

    except Exception as e:
        session.rollback()
        logger.error(f"Error linking email {email_id}: {e}")
        return JSONResponse({"status": "error", "message": str(e)})
    finally:
        session.close()


@router.post("/api/emails/{email_id}/run-pipeline")
async def api_run_email_pipeline(email_id: int, request: Request,
                                  user: dict = Depends(_helpers.require_user)):
    """Trigger the supplier quote pipeline for a specific email (admin only)."""
    if user.get("role") != "Admin":
        return JSONResponse({"status": "error", "message": "Admin only"}, status_code=403)

    session = _helpers.get_session()
    try:
        from includes.dashboard.models import EmailTracking
        tracking = session.query(EmailTracking).filter(EmailTracking.id == email_id).first()
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"}, status_code=404)
        if not (tracking.rfq_token or tracking.rfq_id):
            return JSONResponse({"status": "error", "message": "Email not linked to an RFQ"}, status_code=400)
        if not tracking.supplier_id:
            return JSONResponse({"status": "error", "message": "Email not linked to a supplier"}, status_code=400)

        from includes.tools.supplier_quote_pipeline import trigger_supplier_quote_pipeline
        trigger_supplier_quote_pipeline(email_id, user_id=user.get("email", "admin"))
        logger.info(f"Admin {user.get('email')} triggered pipeline for email #{email_id}")
        return JSONResponse({"status": "ok", "message": f"Pipeline triggered for email #{email_id}"})
    except Exception as e:
        logger.error(f"Error triggering pipeline for email #{email_id}: {e}")
        return JSONResponse({"status": "error", "message": str(e)})
    finally:
        session.close()


def _save_email_domain(session, tracking: "EmailTracking", entity_type: str, entity_id: str) -> str:
    """Extract the external email domain and save it to the entity's alt_domains for future matching."""
    from includes.dashboard.models import Supplier, Customer

    # Determine the external email address
    if tracking.direction == "received":
        external_email = tracking.sender_email or tracking.recipient_email
    else:
        external_email = tracking.recipient_email

    if not external_email or "eagle-exports" in external_email:
        return ""

    domain = external_email.split("@")[-1].lower() if "@" in external_email else None
    if not domain:
        return ""

    # Skip generic providers
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
               "icloud.com", "aol.com", "protonmail.com", "zoho.com", "mail.com",
               "ymail.com", "fastmail.com", "me.com", "msn.com", "googlemail.com",
               "bigpond.com"}
    if domain in generic:
        return f"(skipped generic domain {domain})"

    if entity_type == "supplier":
        supplier = session.query(Supplier).filter(Supplier.id == entity_id).first()
        if supplier:
            existing = supplier.alt_domains or []
            if domain not in existing:
                supplier.alt_domains = existing + [domain]
                return f"Domain '{domain}' saved to {supplier.name}."
            else:
                return f"Domain '{domain}' already registered."
    elif entity_type == "customer":
        # Customers don't have alt_domains, store domain in email field if empty
        customer = session.query(Customer).filter(Customer.id == entity_id).first()
        if customer:
            if not customer.email:
                customer.email = external_email
                return f"Email '{external_email}' saved to {customer.companyname}."
            elif domain in (customer.email or ""):
                return f"Domain already registered via {customer.email}."
            else:
                # Add as a contact instead
                return f"(customer already has email {customer.email}; domain not saved)"

    return ""
