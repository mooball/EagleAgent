"""Admin routes: user management, system admin, job runner, NetSuite status."""

import asyncio
import logging
import math
import uuid
from datetime import datetime, timezone

from fastapi import Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import text

from . import _helpers
from ._helpers import router, templates, require_admin, _render
from .rfqs import _get_store
from includes.dashboard.models import Supplier, SupplierDuplicateCandidate
from includes.dashboard.supplier_dedup import MergeConfig, merge_suppliers

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

_DEDUP_PER_PAGE = 50


def _dedup_status_html(kind: str, message: str) -> str:
    if kind == "error":
        cls = ("bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300 "
               "border-red-200 dark:border-red-800")
    else:
        cls = ("bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300 "
               "border-green-200 dark:border-green-800")
    return (f'<div class="px-4 py-3 rounded-lg text-sm {cls} '
            f'border mb-3">{message}</div>')


def _dedup_supplier_card(sup, txn_stats: dict) -> dict | None:
    if not sup:
        return None
    count, latest = txn_stats.get(sup.id, (0, None))
    scp = sup.supply_chain_position or {}
    contacts = [c for c in (sup.contacts or []) if isinstance(c, dict)]
    return {
        "id": str(sup.id),
        "name": sup.name,
        "netsuite_id": sup.netsuite_id,
        "url": sup.url,
        "country": sup.country,
        "source": sup.source,
        "notes": sup.notes,
        "tier": scp.get("tier"),
        "category": scp.get("category"),
        "contact_count": len(contacts),
        "contacts": [
            {"name": c.get("name"), "email": c.get("email")}
            for c in contacts[:2]
        ],
        "txn_count": count or 0,
        "last_txn": str(latest) if latest is not None else None,
    }


def _dedup_queue_ctx(session, tier: str, page: int, flash=None) -> dict:
    """Build the review queue context: proposed candidates, tiered, paginated."""
    from scripts.scan_supplier_duplicates import candidate_tier

    rows = (
        session.query(SupplierDuplicateCandidate)
        .filter(SupplierDuplicateCandidate.status == "proposed")
        # id asc breaks confidence ties — without it, rows at the same
        # confidence shuffle between queries (after writes) and candidates
        # jump pages, appearing to vanish after a swap.
        .order_by(
            SupplierDuplicateCandidate.confidence.desc().nulls_last(),
            SupplierDuplicateCandidate.id.asc(),
        )
        .all()
    )
    classified = []
    for cand in rows:
        t = candidate_tier(cand.confidence, cand.reasons)
        if tier in ("all", t):
            classified.append((cand, t))

    count_certain = sum(1 for _c, t in classified if t == "certain")
    total = len(classified)
    pages = max(1, math.ceil(total / _DEDUP_PER_PAGE))
    page = max(1, min(page, pages))
    window = classified[(page - 1) * _DEDUP_PER_PAGE: page * _DEDUP_PER_PAGE]

    supplier_ids = []
    for cand, _t in window:
        supplier_ids.extend([cand.primary_id, cand.duplicate_id])
    suppliers = {}
    txn_stats = {}
    if supplier_ids:
        suppliers = {
            s.id: s
            for s in session.query(Supplier).filter(Supplier.id.in_(supplier_ids)).all()
        }
        txn_rows = session.execute(
            text("""
                SELECT supplier_id, count(*) AS cnt, max(date) AS latest
                FROM product_suppliers
                WHERE supplier_id = ANY(:ids)
                GROUP BY supplier_id
            """),
            {"ids": list(set(supplier_ids))},
        ).fetchall()
        txn_stats = {r.supplier_id: (r.cnt, r.latest) for r in txn_rows}

    items = []
    for cand, t in window:
        created_label, _ = _humanize_timestamp(
            cand.created_at.isoformat() if cand.created_at else None
        )
        items.append({
            "id": str(cand.id),
            "tier": t,
            "confidence_pct": round((cand.confidence or 0) * 100),
            "reasons": cand.reasons or [],
            "created_label": created_label,
            "primary": _dedup_supplier_card(suppliers.get(cand.primary_id), txn_stats),
            "duplicate": _dedup_supplier_card(suppliers.get(cand.duplicate_id), txn_stats),
        })

    return {
        "items": items,
        "page": page,
        "pages": pages,
        "total": total,
        "count_certain": count_certain,
        "count_review": total - count_certain,
        "tier": tier,
        "flash": flash,
    }


def _dedup_form_state(form) -> tuple[str, int]:
    tier = form.get("tier", "all")
    try:
        page = int(form.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    return tier, page


@router.get("/admin/duplicates")
async def admin_duplicates(request: Request, user: dict = require_admin) -> HTMLResponse:
    ctx = {"active_nav": "admin"}
    return _render(request, "admin_duplicates.html", "partials/admin_duplicates.html", ctx, user)


@router.get("/partial/admin/duplicates")
async def partial_admin_duplicates(request: Request, user: dict = require_admin) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/admin_duplicates.html", {
        "user": user,
        "active_nav": "admin",
    })


@router.get("/partial/admin/duplicates/list")
async def partial_admin_duplicates_list(request: Request, user: dict = require_admin) -> HTMLResponse:
    tier = request.query_params.get("tier", "all")
    try:
        page = int(request.query_params.get("page", "1"))
    except (TypeError, ValueError):
        page = 1

    session = _helpers.get_session()
    try:
        ctx = _dedup_queue_ctx(session, tier, page)
    finally:
        session.close()
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/_admin_dedup_queue.html", ctx)


@router.post("/admin/duplicates/scan")
async def admin_duplicates_scan(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Kick off the background all-pairs scan (no synchronous wait)."""
    from includes.graph import job_runner
    try:
        job = await job_runner.run_script("scan_supplier_duplicates", [])
    except ValueError as e:
        return HTMLResponse(_dedup_status_html("error", f"Scan not started: {e}"))
    return HTMLResponse(_dedup_status_html(
        "ok", f"Scan started (job {job.id[:8]}). Refresh the queue when it completes."
    ))


@router.post("/admin/duplicates/manual")
async def admin_duplicates_manual_nominate(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Nominate a duplicate pair by hand (source='manual', status='proposed')."""
    import html
    from includes.dashboard.supplier_dedup import pick_keep_remove

    form = await request.form()
    try:
        id_a = uuid.UUID(str(form.get("a_id", "")))
        id_b = uuid.UUID(str(form.get("b_id", "")))
    except ValueError:
        return HTMLResponse(_dedup_status_html("error", "Invalid supplier reference."))

    if id_a == id_b:
        return HTMLResponse(_dedup_status_html("error", "A supplier cannot be a duplicate of itself."))

    session = _helpers.get_session()
    try:
        sup_a = session.get(Supplier, id_a)
        sup_b = session.get(Supplier, id_b)
        if not sup_a or not sup_b:
            return HTMLResponse(_dedup_status_html("error", "Supplier not found."))

        # NetSuite always wins the primary slot — a web record can never
        # be kept over a NetSuite one, so never propose it that way.
        primary, duplicate = pick_keep_remove(sup_a, sup_b)

        row = (
            session.query(SupplierDuplicateCandidate)
            .filter(
                ((SupplierDuplicateCandidate.primary_id == primary.id)
                 & (SupplierDuplicateCandidate.duplicate_id == duplicate.id))
                | ((SupplierDuplicateCandidate.primary_id == duplicate.id)
                   & (SupplierDuplicateCandidate.duplicate_id == primary.id))
            )
            .first()
        )

        now = datetime.now(timezone.utc)
        who = user.get("identifier", "admin")
        if row:
            if row.status == "proposed":
                return HTMLResponse(_dedup_status_html(
                    "error", "This pair is already in the duplicates queue."
                ))
            # Re-open a previously decided pair as a fresh nomination
            row.status = "proposed"
            row.source = "manual"
            row.confidence = 1.0
            row.reasons = ["manual"]
            row.primary_id = primary.id
            row.duplicate_id = duplicate.id
            row.created_by = who
            row.created_at = now
            row.decided_by = None
            row.decided_at = None
        else:
            session.add(SupplierDuplicateCandidate(
                primary_id=primary.id,
                duplicate_id=duplicate.id,
                source="manual",
                status="proposed",
                confidence=1.0,
                reasons=["manual"],
                created_by=who,
                created_at=now,
            ))
        session.commit()
        label_a = html.escape(primary.name or "?")
        label_b = html.escape(duplicate.name or "?")
    finally:
        session.close()

    return HTMLResponse(_dedup_status_html(
        "ok",
        f"Duplicate nominated: <strong>{label_a}</strong> ↔ <strong>{label_b}</strong>. "
        '<a hx-get="/partial/admin/duplicates" hx-target="#main-content" '
        'hx-push-url="/admin/duplicates" class="underline font-medium">'
        "Review in the duplicates queue</a>",
    ))


@router.post("/admin/duplicates/{candidate_id}/merge")
async def admin_duplicates_merge_candidate(
    request: Request, candidate_id: str, user: dict = require_admin
) -> HTMLResponse:
    form = await request.form()
    tier, page = _dedup_form_state(form)
    config = MergeConfig(
        merge_contacts=form.get("merge_contacts") == "1",
        merge_domains=form.get("merge_domains") == "1",
        merge_names=form.get("merge_names") == "1",
    )

    try:
        cand_uuid = uuid.UUID(candidate_id)
    except ValueError:
        return HTMLResponse(_dedup_status_html("error", "Invalid candidate ID."))

    session = _helpers.get_session()
    try:
        cand = session.get(SupplierDuplicateCandidate, cand_uuid)
        if not cand:
            flash = ("error", "Candidate not found.")
        elif cand.status != "proposed":
            flash = ("error", "Candidate already decided.")
        else:
            flash = None
            # Client-side keep toggle is sent with the merge — apply the flip
            if form.get("keep_first", "1") == "0":
                new_primary = session.get(Supplier, cand.duplicate_id)
                new_duplicate = session.get(Supplier, cand.primary_id)
                if (
                    new_primary and not new_primary.netsuite_id
                    and new_duplicate and new_duplicate.netsuite_id
                ):
                    flash = (
                        "error",
                        "Cannot keep the web supplier — the NetSuite record would become "
                        "the duplicate and the merge is rejected.",
                    )
                else:
                    cand.primary_id, cand.duplicate_id = cand.duplicate_id, cand.primary_id
            if flash is None:
                try:
                    result = await asyncio.to_thread(
                        merge_suppliers, session, cand.primary_id, cand.duplicate_id, config
                    )
                except ValueError as e:
                    session.rollback()
                    flash = ("error", f"Merge failed: {e}")
                else:
                    cand.status = "merged"
                    cand.decided_by = user.get("identifier", "admin")
                    cand.decided_at = datetime.now(timezone.utc)
                    session.commit()
        ctx = _dedup_queue_ctx(session, tier, page, flash=flash)
    finally:
        session.close()
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/_admin_dedup_queue.html", ctx)


@router.post("/admin/duplicates/{candidate_id}/reject")
async def admin_duplicates_reject_candidate(
    request: Request, candidate_id: str, user: dict = require_admin
) -> HTMLResponse:
    form = await request.form()
    tier, page = _dedup_form_state(form)

    try:
        cand_uuid = uuid.UUID(candidate_id)
    except ValueError:
        return HTMLResponse(_dedup_status_html("error", "Invalid candidate ID."))

    session = _helpers.get_session()
    try:
        cand = session.get(SupplierDuplicateCandidate, cand_uuid)
        if not cand:
            flash = ("error", "Candidate not found.")
        elif cand.status != "proposed":
            flash = ("error", "Candidate already decided.")
        else:
            cand.status = "rejected"
            cand.decided_by = user.get("identifier", "admin")
            cand.decided_at = datetime.now(timezone.utc)
            session.commit()
            flash = None
        ctx = _dedup_queue_ctx(session, tier, page, flash=flash)
    finally:
        session.close()
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/_admin_dedup_queue.html", ctx)


@router.post("/admin/duplicates/{candidate_id}/swap")
async def admin_duplicates_swap_candidate(
    request: Request, candidate_id: str, user: dict = require_admin
) -> HTMLResponse:
    """Flip which side is kept (primary). Blocked when it would make the
    NetSuite supplier the duplicate — the merge matrix rejects that."""
    form = await request.form()
    tier, page = _dedup_form_state(form)

    try:
        cand_uuid = uuid.UUID(candidate_id)
    except ValueError:
        return HTMLResponse(_dedup_status_html("error", "Invalid candidate ID."))

    session = _helpers.get_session()
    try:
        cand = session.get(SupplierDuplicateCandidate, cand_uuid)
        if not cand:
            flash = ("error", "Candidate not found.")
        elif cand.status != "proposed":
            flash = ("error", "Candidate already decided.")
        else:
            new_primary = session.get(Supplier, cand.duplicate_id)
            new_duplicate = session.get(Supplier, cand.primary_id)
            if (
                new_primary and not new_primary.netsuite_id
                and new_duplicate and new_duplicate.netsuite_id
            ):
                flash = (
                    "error",
                    "Cannot keep the web supplier — the NetSuite record would become "
                    "the duplicate and the merge is rejected.",
                )
            else:
                cand.primary_id, cand.duplicate_id = cand.duplicate_id, cand.primary_id
                session.commit()
                flash = (
                    "ok",
                    f"Swapped — {new_primary.name if new_primary else 'the other supplier'} will be kept.",
                )
        ctx = _dedup_queue_ctx(session, tier, page, flash=flash)
    finally:
        session.close()
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/_admin_dedup_queue.html", ctx)


@router.post("/admin/duplicates/bulk-merge")
async def admin_duplicates_bulk_merge(request: Request, user: dict = require_admin) -> HTMLResponse:
    form = await request.form()
    tier, page = _dedup_form_state(form)
    candidate_ids = [i for i in form.getlist("ids") if i]
    config = MergeConfig(
        merge_contacts=form.get("merge_contacts") == "1",
        merge_domains=form.get("merge_domains") == "1",
        merge_names=form.get("merge_names") == "1",
    )

    session = _helpers.get_session()
    try:
        merged = 0
        errors = []
        for cid in candidate_ids:
            try:
                cand_uuid = uuid.UUID(cid)
            except ValueError:
                continue
            cand = session.get(SupplierDuplicateCandidate, cand_uuid)
            if not cand or cand.status != "proposed":
                continue
            try:
                await asyncio.to_thread(
                    merge_suppliers, session, cand.primary_id, cand.duplicate_id, config
                )
            except ValueError as e:
                session.rollback()
                errors.append(str(e))
            else:
                cand.status = "merged"
                cand.decided_by = user.get("identifier", "admin")
                cand.decided_at = datetime.now(timezone.utc)
                session.commit()
                merged += 1

        if errors:
            flash = ("error", f"Merged {merged}; failed: {', '.join(errors[:3])}")
        else:
            flash = None
        ctx = _dedup_queue_ctx(session, tier, page, flash=flash)
    finally:
        session.close()
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/_admin_dedup_queue.html", ctx)


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


def _is_email_all_internal(sender_email: str | None, recipient_email: str | None, user_email: str | None) -> bool:
    """Return True if ALL parties on an email are internal or generic domains."""
    from includes.gmail.matching import _INTERNAL_DOMAINS, _GENERIC_DOMAINS
    all_internal = _INTERNAL_DOMAINS | _GENERIC_DOMAINS
    addresses: list[str] = []
    if sender_email:
        addresses.append(sender_email.strip())
    if recipient_email:
        for addr in recipient_email.split(","):
            a = addr.strip()
            if a:
                addresses.append(a)
    if user_email:
        addresses.append(user_email.strip())
    if not addresses:
        return False
    for addr in addresses:
        domain = addr.split("@")[-1].lower() if "@" in addr else ""
        if domain and domain not in all_internal:
            return False
    return True


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
                et.rfq_creation_result,
                et.feedback,
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
        e["is_internal"] = _is_email_all_internal(
            e.get("sender_email"), e.get("recipient_email"), e.get("user_email")
        )
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

@router.get("/api/admin/match-email")
async def api_match_email(request: Request, email: str, user: dict = Depends(_helpers.require_user)):
    """Suggest entity matches for an email address (exact email → domain fallback).

    Returns candidates for the admin email linking UI.
    """
    if not email or "@" not in email:
        return JSONResponse({"candidates": [], "is_unique": False})

    from includes.gmail.matching import find_all_matches

    session = _helpers.get_session()
    try:
        result = find_all_matches(session, email)
        return JSONResponse({
            "candidates": result["candidates"],
            "is_unique": result["is_unique"],
            "match_type": result["match_type"],
        })
    finally:
        session.close()


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


@router.post("/api/admin/create-rfq")
async def api_create_rfq(request: Request, user: dict = Depends(_helpers.require_user)):
    """Create an RFQ from an email in the dashboard (same pipeline as Gmail add-on)."""
    from includes.dashboard.models import EmailTracking

    body = await request.json()
    email_id = body.get("email_id")
    if not email_id:
        return JSONResponse({"status": "error", "message": "Missing email_id"}, status_code=400)

    session = _helpers.get_session()
    try:
        tracking = session.query(EmailTracking).filter(EmailTracking.id == email_id).first()
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"}, status_code=404)

        if not tracking.customer_id:
            return JSONResponse({"status": "error", "message": "No customer linked. Link a customer first."}, status_code=400)

        if tracking.rfq_token or tracking.rfq_id:
            return JSONResponse({
                "status": "error",
                "message": f"Already linked to RFQ {tracking.rfq_token or tracking.rfq_id}"
            }, status_code=400)

        if tracking.rfq_creation_result:
            existing = tracking.rfq_creation_result
            if existing.get("status") == "error":
                return JSONResponse({
                    "status": "error",
                    "message": f"Previous attempt failed: {existing.get('error', 'unknown')}"
                }, status_code=400)
            return JSONResponse({
                "status": "ok",
                "message": f"RFQ already created: {existing.get('rfq_number', 'unknown')}",
            })

        user_ident = user.get("email", user.get("identifier", "dashboard"))
        from includes.tools.rfq_creation_pipeline import trigger_rfq_creation_pipeline
        trigger_rfq_creation_pipeline(tracking.id, user_id=user_ident)

        return JSONResponse({
            "status": "processing",
            "message": "RFQ creation started. Refresh the page to see the new RFQ.",
        })

    except Exception as exc:
        session.rollback()
        logger.exception("Error creating RFQ from dashboard")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)
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
            return JSONResponse({"status": "ok", "message": f"Linked to {name}. {domain_msg}".strip(), "entity_name": name})

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
            return JSONResponse({"status": "ok", "message": f"Linked to {name}. {domain_msg}".strip(), "entity_name": name})

        else:
            return JSONResponse({"status": "error", "message": f"Unknown link type: {link_type}"})

    except Exception as e:
        session.rollback()
        logger.error(f"Error linking email {email_id}: {e}")
        return JSONResponse({"status": "error", "message": str(e)})
    finally:
        session.close()


@router.post("/api/admin/unlink-email")
async def api_unlink_email(request: Request, user: dict = Depends(_helpers.require_user)):
    """Unlink an email (and its thread) from a customer, supplier, or RFQ."""
    from includes.dashboard.models import EmailTracking

    body = await request.json()
    email_id = body.get("email_id")
    link_type = body.get("link_type")  # 'rfq', 'customer', 'supplier'

    if not email_id or not link_type:
        return JSONResponse({"status": "error", "message": "Missing email_id or link_type"}, status_code=400)
    if link_type not in ("rfq", "customer", "supplier"):
        return JSONResponse({"status": "error", "message": "link_type must be 'rfq', 'customer', or 'supplier'"}, status_code=400)

    session = _helpers.get_session()
    try:
        tracking = session.query(EmailTracking).filter(EmailTracking.id == email_id).first()
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"}, status_code=404)

        tid = tracking.gmail_thread_id or ""

        if link_type == "customer":
            session.execute(
                text("UPDATE email_tracking SET customer_id = NULL WHERE gmail_thread_id = :tid OR id = :eid"),
                {"tid": tid, "eid": email_id},
            )
            session.commit()
            return JSONResponse({"status": "ok", "message": "Unlinked from customer"})

        elif link_type == "supplier":
            session.execute(
                text("UPDATE email_tracking SET supplier_id = NULL WHERE gmail_thread_id = :tid OR id = :eid"),
                {"tid": tid, "eid": email_id},
            )
            session.commit()
            return JSONResponse({"status": "ok", "message": "Unlinked from supplier"})

        elif link_type == "rfq":
            session.execute(
                text("UPDATE email_tracking SET rfq_token = NULL, rfq_id = NULL WHERE gmail_thread_id = :tid OR id = :eid"),
                {"tid": tid, "eid": email_id},
            )
            session.commit()
            return JSONResponse({"status": "ok", "message": "Unlinked from RFQ"})

    except Exception as e:
        session.rollback()
        logger.error(f"Error unlinking email {email_id}: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
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


@router.post("/api/emails/{email_id}/feedback")
async def api_email_feedback(email_id: int, request: Request,
                              user: dict = Depends(_helpers.require_user)):
    """Save user feedback about a supplier quote pipeline result.

    Body: {"text": "..."} — stored on the email_tracking.feedback JSONB column
    together with a snapshot of the pipeline result at feedback time.
    """
    from datetime import datetime, timezone
    from includes.dashboard.models import EmailTracking

    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid JSON body"}, status_code=400)

    if not text:
        return JSONResponse({"status": "error", "message": "Feedback text is required"}, status_code=400)

    session = _helpers.get_session()
    try:
        tracking = session.query(EmailTracking).filter(EmailTracking.id == email_id).first()
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"}, status_code=404)

        spr = tracking.supplier_pipeline_result or {}
        feedback = {
            "text": text,
            "user": user.get("email", ""),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": {
                "classification": spr.get("classification"),
                "reason": spr.get("reason"),
                "processed_at": spr.get("processed_at"),
            },
        }
        tracking.feedback = feedback
        session.commit()
        logger.info(f"Feedback saved for email #{email_id} by {user.get('email')}")
        return JSONResponse({"status": "ok", "feedback": feedback})
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving feedback for email #{email_id}: {e}")
        return JSONResponse({"status": "error", "message": str(e)})
    finally:
        session.close()


@router.post("/api/emails/{email_id}/re-extract-rfq")
async def api_re_extract_rfq(email_id: int, request: Request,
                              user: dict = Depends(_helpers.require_user)):
    """Re-run RFQ item extraction for an already-linked email (admin only).

    Runs only the Stage 3 extraction (email content → items + title + notes)
    and applies the results to the existing RFQ. Does NOT create a new RFQ.
    """
    if user.get("role") != "Admin":
        return JSONResponse({"status": "error", "message": "Admin only"}, status_code=403)

    from includes.dashboard.database import get_session
    from includes.dashboard.models import EmailTracking
    from includes.tools.rfq_creation_pipeline import _extract_rfq_items_sync, _save_rfq_creation_result, _now_iso

    session = get_session()
    try:
        tracking = session.query(EmailTracking).get(email_id)
        if not tracking:
            return JSONResponse({"status": "error", "message": "Email not found"}, status_code=404)

        rfq_number = tracking.rfq_token or tracking.rfq_id
        if not rfq_number:
            return JSONResponse({"status": "error", "message": "Email not linked to an RFQ"}, status_code=400)

        # Run extraction
        items, llm_result = _extract_rfq_items_sync(email_id)

        # Apply items to the RFQ
        if items:
            from includes.tools.rfq_crud import _add_items_sync
            try:
                _add_items_sync(rfq_number=rfq_number, data={"items": items}, user_id=user.get("email", "admin"))
            except Exception as e:
                logger.warning(f"Re-extract #{email_id}: failed to add items — {e}")

        # Apply title and notes
        if llm_result:
            from includes.tools.rfq_crud import _update_rfq_sync
            updates = {}
            if llm_result.get("title"):
                updates["title"] = llm_result["title"]
            if llm_result.get("customer_notes"):
                updates["notes"] = llm_result["customer_notes"]
            if updates:
                try:
                    _update_rfq_sync(rfq_number, updates, user.get("email", "admin"))
                except Exception as e:
                    logger.warning(f"Re-extract #{email_id}: failed to update title/notes — {e}")

        # Save result for the badge/popup
        customer_name = str(tracking.customer_id) if tracking.customer_id else ""
        result = {
            "rfq_number": rfq_number,
            "items_extracted": len(items),
            "customer": customer_name,
            "status": "complete",
            "extraction_method": "gemini_llm",
            "title": llm_result.get("title", "") if llm_result else "",
            "customer_notes": llm_result.get("customer_notes", "") if llm_result else "",
            "raw_items": items,
            "warnings": llm_result.get("warnings", []) if llm_result else [],
            "actions": [f"Re-extracted: {len(items)} items found"],
            "processed_at": _now_iso(),
        }
        if llm_result and llm_result.get("_raw_response"):
            result["llm_raw_response"] = llm_result["_raw_response"]
        if llm_result and llm_result.get("error"):
            result["extraction_error"] = llm_result["error"]
            result["status"] = "error"
        if not items and result["status"] != "error":
            result["warnings"].append("No items could be extracted from the email content.")

        _save_rfq_creation_result(email_id, result)

        logger.info(f"Admin {user.get('email')} re-extracted RFQ items for email #{email_id}: {len(items)} items")
        # Return a small HTML indicator — the button swaps itself out
        indicator = f"✓ {len(items)}" if items else "✓ 0"
        title = f"Found {len(items)} items" if items else "Extraction complete — no items found"
        return HTMLResponse(
            f'<span class="shrink-0 inline-flex items-center px-1.5 py-0 text-[10px] font-medium rounded-full '
            f'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300" '
            f'title="{title}">{indicator}</span>'
        )
    except Exception as e:
        logger.exception(f"Error re-extracting RFQ for email #{email_id}")
        return JSONResponse({"status": "error", "message": str(e)})
    finally:
        session.close()


def _save_email_domain(session, tracking: "EmailTracking", entity_type: str, entity_id: str) -> str:
    """Extract the external email domain and save it to the entity for future matching."""
    from includes.gmail.matching import save_sender_domain

    # Determine the external email address
    if tracking.direction == "received":
        external_email = tracking.sender_email or tracking.recipient_email
    else:
        external_email = tracking.recipient_email

    if not external_email or "eagle-exports" in external_email:
        return ""

    return save_sender_domain(session, external_email, entity_type, entity_id)


# ---------------------------------------------------------------------------
# Employee Mappings — manage netsuite_employee_mappings table
# ---------------------------------------------------------------------------

@router.get("/admin/employee-mappings")
async def admin_employee_mappings(request: Request, user: dict = require_admin) -> HTMLResponse:
    session = _helpers.get_session()
    try:
        mappings = _get_all_mappings(session)
        ctx = {"active_nav": "admin", "mappings": mappings}
        return _render(request, "admin_employee_mappings.html", "partials/admin_employee_mappings.html", ctx, user)
    finally:
        session.close()


@router.get("/partial/admin/employee-mappings")
async def partial_admin_employee_mappings(request: Request, user: dict = require_admin) -> HTMLResponse:
    session = _helpers.get_session()
    try:
        mappings = _get_all_mappings(session)
        return templates.TemplateResponse(request, "partials/admin_employee_mappings.html", {
            "user": user, "active_nav": "admin", "mappings": mappings, "unmapped": None,
        })
    finally:
        session.close()


@router.post("/admin/employee-mappings/scan")
async def admin_employee_mappings_scan(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Scan NetSuite for employees and return any not yet in the mapping table."""
    from includes.netsuite.client import NetSuiteClient

    session = _helpers.get_session()
    try:
        existing_mappings = _get_all_mappings(session)
        existing_ids = {str(m["netsuite_employee_id"]) for m in existing_mappings}

        # Query NetSuite for employees from transactions
        client = NetSuiteClient()
        tx_query = (
            "SELECT DISTINCT employee, BUILTIN.DF(employee) AS employee_name "
            "FROM transaction WHERE employee IS NOT NULL ORDER BY employee"
        )
        resp = client.post("query/v1/suiteql", json={"q": tx_query}, params={"limit": 1000, "offset": 0})
        ns_employees = resp.json().get("items", [])

        unmapped = [
            {"employee_id": e["employee"], "employee_name": e["employee_name"]}
            for e in ns_employees
            if str(e["employee"]) not in existing_ids
        ]

        return templates.TemplateResponse(request, "partials/_admin_employee_mapping_scan.html", {
            "user": user, "unmapped": unmapped,
        })
    except Exception as e:
        logger.exception("Employee mapping scan failed")
        return HTMLResponse(
            f'<div class="text-red-500 text-sm p-2">Scan failed: {e}</div>'
        )
    finally:
        session.close()


@router.post("/admin/employee-mappings/save")
async def admin_employee_mappings_save(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Add or update a netsuite_employee_mapping entry."""
    from includes.dashboard.models import NetSuiteEmployeeMapping
    from sqlalchemy.orm.attributes import flag_modified

    form = await request.form()
    ns_id = (form.get("netsuite_id", "") or "").strip()
    name = (form.get("name", "") or "").strip()
    email = (form.get("email", "") or "").strip().lower() or None
    is_active = form.get("is_active", "1") == "1"

    if not ns_id or not name:
        return HTMLResponse('<div class="text-red-500 text-sm p-2">Name and NetSuite ID are required.</div>')

    session = _helpers.get_session()
    try:
        existing = session.query(NetSuiteEmployeeMapping).filter(
            NetSuiteEmployeeMapping.netsuite_employee_id == ns_id
        ).first()

        if existing:
            existing.name = name
            existing.email = email
            existing.is_active = is_active
            session.commit()
            return HTMLResponse(
                '<div class="text-green-600 text-sm p-2">✓ Updated.</div>',
                headers={"HX-Trigger": "mapping-saved"}
            )
        else:
            new_mapping = NetSuiteEmployeeMapping(
                netsuite_employee_id=ns_id,
                name=name,
                email=email,
                is_active=is_active,
            )
            session.add(new_mapping)
            session.commit()
            return HTMLResponse(
                '<div class="text-green-600 text-sm p-2">✓ Added.</div>',
                headers={"HX-Trigger": "mapping-saved"}
            )
    except Exception as e:
        session.rollback()
        logger.exception("Save mapping failed")
        return HTMLResponse(f'<div class="text-red-500 text-sm p-2">Error: {e}</div>')
    finally:
        session.close()


@router.post("/admin/employee-mappings/toggle")
async def admin_employee_mappings_toggle(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Toggle is_active for a mapping entry."""
    from includes.dashboard.models import NetSuiteEmployeeMapping

    form = await request.form()
    ns_id = (form.get("netsuite_id", "") or "").strip()

    session = _helpers.get_session()
    try:
        mapping = session.query(NetSuiteEmployeeMapping).filter(
            NetSuiteEmployeeMapping.netsuite_employee_id == ns_id
        ).first()
        if mapping:
            mapping.is_active = not mapping.is_active
            session.commit()
            new_state = "active" if mapping.is_active else "inactive"
            return HTMLResponse(
                f'<span class="text-xs {"text-green-600" if mapping.is_active else "text-gray-400"}">{new_state}</span>',
                headers={"HX-Trigger": "mapping-saved"}
            )
        return HTMLResponse("")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# System Settings — read-only viewer for the system_settings table
# ---------------------------------------------------------------------------

@router.get("/admin/system-settings")
async def admin_system_settings(request: Request, user: dict = require_admin) -> HTMLResponse:
    from includes.system_settings import list_settings

    session = _helpers.get_session()
    try:
        settings = list_settings(session)
        ctx = {"active_nav": "admin", "settings": settings}
        return _render(request, "admin_system_settings.html", "partials/admin_system_settings.html", ctx, user)
    finally:
        session.close()


@router.get("/partial/admin/system-settings")
async def partial_admin_system_settings(request: Request, user: dict = require_admin) -> HTMLResponse:
    from includes.system_settings import list_settings

    session = _helpers.get_session()
    try:
        settings = list_settings(session)
        return templates.TemplateResponse(request, "partials/admin_system_settings.html", {
            "user": user, "active_nav": "admin", "settings": settings,
        })
    finally:
        session.close()


@router.get("/partial/admin/system-settings/{key}")
async def partial_admin_system_setting_detail(request: Request, user: dict = require_admin) -> HTMLResponse:
    """Return the full JSON value for a single setting."""
    import json
    from includes.system_settings import get_setting

    key = request.path_params["key"]
    session = _helpers.get_session()
    try:
        value = get_setting(session, key)
        pretty = json.dumps(value, indent=2, default=str, ensure_ascii=False)
        return HTMLResponse(
            f'<pre class="text-xs text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 p-3 rounded overflow-x-auto max-h-96 overflow-y-auto"><code>{pretty}</code></pre>'
        )
    finally:
        session.close()


def _get_all_mappings(session) -> list[dict]:
    """Get all netsuite_employee_mappings as dicts."""
    rows = session.execute(
        text("""
            SELECT netsuite_employee_id, name, email, is_active
            FROM netsuite_employee_mappings
            ORDER BY is_active DESC, name
        """)
    ).mappings().all()
    return [dict(r) for r in rows]
