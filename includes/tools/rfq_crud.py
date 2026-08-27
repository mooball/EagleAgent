"""
RFQ CRUD helpers — synchronous DB operations called via asyncio.to_thread.

All public names are re-exported from quote_tools.py for backward compatibility.
"""

import datetime
import logging
import threading

from functools import wraps
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from includes.tools.product_tools import normalize_part_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-RFQ write serialization
# ---------------------------------------------------------------------------
# The quote tools run these sync helpers in parallel threads (LangGraph
# gathers tool calls, each helper runs in its own thread + transaction).
# Two concurrent writers on the same RFQ deadlocked on Postgres row locks
# and silently lost read-modify-write history updates. Serialize writes
# per RFQ so different RFQs stay parallel but one RFQ never has two
# concurrent writers.
_rfq_write_locks: dict[str, "threading.RLock"] = {}
_rfq_write_locks_guard = threading.Lock()


def _rfq_write_lock(rfq_number: str) -> "threading.RLock":
    with _rfq_write_locks_guard:
        lock = _rfq_write_locks.get(rfq_number)
        if lock is None:
            lock = threading.RLock()
            _rfq_write_locks[rfq_number] = lock
        return lock


def _serialized_rfq_write(fn):
    """Decorator: serialize a write helper per RFQ (first arg is rfq_number)."""
    @wraps(fn)
    def wrapper(rfq_number, *args, **kwargs):
        with _rfq_write_lock(str(rfq_number)):
            return fn(rfq_number, *args, **kwargs)
    return wrapper


# Common aliases LLMs use for the "line" parameter
_LINE_ALIASES = ("line", "line_number", "item", "item_number")


def _get_line(data: dict):
    """Extract line number from data, accepting common aliases."""
    for key in _LINE_ALIASES:
        if key in data:
            return data[key]
    return None


# Part number values that should be treated as empty
_EMPTY_PART_NUMBERS = frozenset({"tbd", "-", "\u2013", "\u2014", "n/a", "na", "none", "?", ""})


def _is_empty_part_number(pn) -> bool:
    """Return True if the part number is effectively empty (null, blank, or placeholder)."""
    if not pn:
        return True
    return str(pn).strip().lower() in _EMPTY_PART_NUMBERS


def _resolve_department(data: dict) -> str | None:
    """Resolve department input to a canonical enum ID.

    Accepts ``department_id`` (NetSuite internal ID, e.g. '5') and/or
    ``department`` (exact case-insensitive label, e.g. 'Truck Parts').
    Empty values clear. Returns None to clear, or the canonical ID string.
    Raises ValueError on unknown IDs/labels or conflicting inputs.
    """
    from includes.netsuite.departments import DEPARTMENT_BY_ID, DEPARTMENT_BY_LABEL

    raw_id = data.get("department_id")
    raw_label = data.get("department")
    dept_id = None
    if raw_id not in (None, ""):
        dept_id = str(raw_id).strip()
        if dept_id not in DEPARTMENT_BY_ID:
            raise ValueError(
                f"Invalid department_id '{dept_id}' — must be one of: "
                f"{', '.join(DEPARTMENT_BY_ID)}"
            )
    if raw_label not in (None, ""):
        resolved = DEPARTMENT_BY_LABEL.get(str(raw_label).strip().lower())
        if resolved is None:
            raise ValueError(
                f"Invalid department '{raw_label}' — must be one of: "
                f"{', '.join(d.label for d in DEPARTMENT_BY_ID.values())}"
            )
        if dept_id is not None and resolved.value != dept_id:
            raise ValueError(
                f"Conflicting department inputs: id '{dept_id}' vs "
                f"label '{raw_label}'"
            )
        dept_id = resolved.value
    return dept_id


def _now_iso() -> str:
    """Return current AEST (UTC+10) timestamp in ISO format."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).isoformat(timespec="seconds")


def _today() -> str:
    """Return current AEST date as YYYY-MM-DD."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).strftime("%Y-%m-%d")


def _today_date():
    """Return current AEST date as a date object."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).date()


def _now_dt():
    """Return current AEST datetime (timezone-aware)."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    )


# ---------------------------------------------------------------------------
# DB session helper
# ---------------------------------------------------------------------------

def _get_session():
    from includes.dashboard.database import get_session
    return get_session()


# ---------------------------------------------------------------------------
# ORM ↔ dict conversion
# ---------------------------------------------------------------------------

def _next_rfq_number_sync() -> str:
    """Generate the next sequential RFQ number like RFQ-2026-0042."""
    from includes.dashboard.models import RFQ
    year = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).year
    prefix = f"RFQ-{year}-"

    session = _get_session()
    try:
        from sqlalchemy import func
        result = session.query(func.max(RFQ.rfq_number)).filter(
            RFQ.rfq_number.like(f"{prefix}%")
        ).scalar()
        if result:
            try:
                seq = int(result[len(prefix):])
            except ValueError:
                seq = 0
        else:
            seq = 0
        return f"{prefix}{seq + 1:04d}"
    finally:
        session.close()


def _get_rfq_suppliers(items: list) -> list[dict]:
    """Return unique shortlisted/selected suppliers across RFQ items.

    Deduplicated by supplier_id (fallback: name.lower()).
    Each entry: supplier_id, name, status, quote_status, lines.

    Used by _rfq_to_dict for listing counts, _build_rfq_supplier_email_data
    for the suppliers tab, and _build_quotation_snapshot for the quotation tab.
    """
    suppliers: dict[str, dict] = {}
    for item in items:
        for s in (item.get("suppliers") or []):
            name = (s.get("name") or "").strip()
            if not name:
                continue
            status = s.get("status", "")
            if status not in ("shortlisted", "selected"):
                continue
            sid = s.get("supplier_id")
            key = str(sid) if sid else name.lower()
            if key not in suppliers:
                suppliers[key] = {
                    "supplier_id": sid,
                    "name": name,
                    "status": status,
                    "quote_status": s.get("quote_status"),
                    "contacts": s.get("contacts") or [],
                    "country": s.get("country"),
                    "currency": s.get("currency"),
                    "lines": [],
                }
            # Track the best quote_status (a supplier may be quoted on one line but not another)
            existing_qs = suppliers[key]["quote_status"]
            new_qs = s.get("quote_status")
            if existing_qs != "quoted" and new_qs == "quoted":
                suppliers[key]["quote_status"] = "quoted"
            suppliers[key]["lines"].append(item.get("line"))
    return list(suppliers.values())


def _rfq_to_dict(rfq) -> dict:
    """Convert an RFQ ORM object (with items loaded) to a plain dict
    compatible with the rendering functions."""
    from datetime import datetime, timezone, date as date_type
    created = rfq.created_date  # DateTime(timezone=True) after migration, Date before
    created_str = ""
    created_display = ""
    if created:
        # Handle migration transition: may be datetime or date
        is_datetime = hasattr(created, 'tzinfo')
        if is_datetime and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_str = created.strftime("%Y-%m-%d") if is_datetime else str(created)
        now = datetime.now(timezone.utc)
        if is_datetime:
            delta = now - created
            hour = (created.hour % 12) or 12
            ampm = "am" if created.hour < 12 else "pm"
            time_str = f"{hour}:{created.minute:02d}{ampm}"
        else:
            # Pre-migration: no time component, use midnight as reference
            delta = now - datetime.combine(created, datetime.min.time(), tzinfo=timezone.utc)
            time_str = ""
        hours = int(delta.total_seconds() / 3600)
        age = f"{hours}h"
        age_hours = hours
        if time_str:
            created_display = f"{created_str} {time_str} ({age})"
        else:
            created_display = f"{created_str} ({age})"
    else:
        age_hours = 0
    items = [_item_to_dict(item) for item in (rfq.items or [])]
    supplier_list = _get_rfq_suppliers(items)
    supplier_count = len(supplier_list)
    quoted_count = sum(1 for s in supplier_list if s["quote_status"] == "quoted")
    return {
        "id": rfq.rfq_number,
        "rfq_number": rfq.rfq_number,
        "customer": rfq.customer,
        "customer_id": str(rfq.customer_id) if rfq.customer_id else None,
        "customer_contact": rfq.customer_contact,
        "reference": rfq.reference,
        "netsuite_opportunity": rfq.netsuite_opportunity,
        "opportunity_id": str(rfq.opportunity_id) if rfq.opportunity_id else None,
        "hubspot_deal": rfq.hubspot_deal,
        "quote_brand_id": str(rfq.quote_brand_id) if rfq.quote_brand_id else None,
        "quote_brand": rfq.quote_brand or "",
        "created_by": rfq.created_by,
        "created_date": created_str,
        "created_display": created_display,
        "age_hours": age_hours,
        "supplier_count": supplier_count,
        "quoted_count": quoted_count,
        "assigned_to": rfq.assigned_to,
        "thread_id": rfq.thread_id,
        "status": rfq.status or "draft",
        "title": rfq.title or "",
        "notes": rfq.notes or "",         # customer requirements, delivery dates, context
        "history": rfq.history or [],
        "item_groups": rfq.item_groups,
        "pipeline_stage": getattr(rfq, "pipeline_stage", "unprocessed") or "unprocessed",
        "supplier_meta": rfq.supplier_meta or {},
        "items": items,
    }


def _item_to_dict(item) -> dict:
    """Convert an RFQItem ORM object to a plain dict."""
    from includes.netsuite.departments import DEPARTMENT_BY_ID

    department_id = item.department_id or None
    department = None
    if department_id and department_id in DEPARTMENT_BY_ID:
        department = DEPARTMENT_BY_ID[department_id].label
    return {
        "line": item.line,
        "input_description": item.input_description or "",
        "input_code": item.input_code or "",
        "part_number": item.part_number,
        "brand": item.brand,
        "department_id": department_id,
        "department": department,
        "product_id": str(item.product_id) if item.product_id else None,
        "quantity": item.quantity,
        "uom": item.uom or "ea",
        "match": item.match or "unmatched",
        "notes": item.notes or "",
        "cost_price": float(item.cost_price) if item.cost_price else None,
        "sale_price": float(item.sale_price) if item.sale_price else None,
        "suppliers": item.suppliers or [],
        "brand_suppliers": item.brand_suppliers or [],
    }


def _get_rfq_sync(rfq_number: str):
    """Fetch a single RFQ by rfq_number, with items eagerly loaded. Returns (rfq_orm, session)."""
    from includes.dashboard.models import RFQ
    session = _get_session()
    rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
    return rfq, session


# ---------------------------------------------------------------------------
# Supplier sorting
# ---------------------------------------------------------------------------

_TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


def _supplier_sort_key(sup: dict) -> tuple:
    """Sort key for ordering suppliers on an RFQ line item.

    Priority (ascending):
      1. Transaction history — suppliers with history first
      2. Supply chain tier — A > B > C > D > unknown
      3. Location — AU first, non-AU second
      4. Alphabetical by name
    """
    has_history = 0 if (sup.get("transaction_count") or sup.get("purchase_ref")) else 1
    tier = _TIER_ORDER.get(sup.get("tier"), 9)
    is_au = 0 if (sup.get("country") or "").upper() == "AU" else 1
    name = (sup.get("name") or "").lower()
    return (has_history, tier, is_au, name)


def sort_item_suppliers(suppliers: list[dict]) -> list[dict]:
    """Return a sorted copy of a supplier list. Safe to call at any time."""
    return sorted(suppliers, key=_supplier_sort_key)


def _enrich_suppliers_from_db(suppliers: list[dict], session) -> None:
    """Fill in missing tier and country on supplier dicts from the Supplier DB.

    Mutates supplier dicts in-place. Only queries the DB for suppliers that
    have a supplier_id but are missing tier or country.
    """
    from includes.dashboard.models import Supplier
    import uuid

    ids_to_lookup = {}  # str(uuid) -> list of supplier dicts
    for sup in suppliers:
        sid = sup.get("supplier_id")
        if not sid:
            continue
        missing_tier = not sup.get("tier")
        missing_country = not sup.get("country")
        if missing_tier or missing_country:
            ids_to_lookup.setdefault(str(sid), []).append(sup)

    if not ids_to_lookup:
        return

    # Batch query all needed supplier IDs
    try:
        uuids = [uuid.UUID(s) for s in ids_to_lookup]
    except (ValueError, TypeError):
        return

    rows = session.query(
        Supplier.id, Supplier.country, Supplier.supply_chain_position,
    ).filter(Supplier.id.in_(uuids)).all()

    for row in rows:
        sid_str = str(row.id)
        scp = row.supply_chain_position or {}
        db_tier = scp.get("tier")
        db_country = row.country

        for sup in ids_to_lookup.get(sid_str, []):
            if not sup.get("tier") and db_tier:
                sup["tier"] = db_tier
            if not sup.get("country") and db_country:
                sup["country"] = db_country


@_serialized_rfq_write
def _sort_rfq_suppliers_sync(rfq_number: str) -> dict | None:
    """Sort suppliers on every line item of an RFQ. Returns RFQ dict or None.

    Also enriches suppliers with tier/country from the Supplier DB table
    when those fields are missing, so the sort has accurate data.
    """
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return None

        changed = False
        for item in rfq.items or []:
            if item.suppliers:
                _enrich_suppliers_from_db(item.suppliers, session)
                item.suppliers = sort_item_suppliers(item.suppliers)
                flag_modified(item, "suppliers")
                changed = True

        if changed:
            session.commit()
            session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Sync mutation helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _create_rfq_sync(data: dict, user_id: str) -> dict:
    """Create an RFQ + items in SQL. Returns the RFQ as a dict."""
    from includes.dashboard.models import RFQ, RFQItem

    customer = data.get("customer")
    if customer is None:
        return {"error": "Error: 'customer' is required in data when creating an RFQ."}

    new_number = _next_rfq_number_sync()
    now = _now_iso()
    raw_items = data.get("items", [])

    history_action = "Created RFQ"
    if raw_items:
        history_action = f"Created RFQ with {len(raw_items)} items"

    session = _get_session()
    try:
        rfq = RFQ(
            rfq_number=new_number,
            customer=customer,
            customer_id=data.get("customer_id"),
            customer_contact=data.get("customer_contact"),
            reference=data.get("reference"),
            netsuite_opportunity=data.get("netsuite_opportunity"),
            hubspot_deal=data.get("hubspot_deal"),
            created_by=user_id,
            created_date=_now_dt(),
            assigned_to=data.get("assigned_to", user_id),
            thread_id=data.get("thread_id"),
            status=data.get("status", "draft"),
            title=data.get("title", ""),
            notes=data.get("notes", ""),
            history=[{"date": now, "user": user_id, "action": history_action}],
            updated_at=_now_dt(),
        )
        session.add(rfq)
        session.flush()  # get rfq.id

        for idx, raw in enumerate(raw_items, start=1):
            item = RFQItem(
                rfq_id=rfq.id,
                line=idx,
                input_description=raw.get("input_description", ""),
                input_code=raw.get("input_code", ""),
                part_number=raw.get("part_number"),
                brand=raw.get("brand"),
                product_id=raw.get("product_id"),
                quantity=raw.get("quantity"),
                uom=raw.get("uom", "ea"),
                match=raw.get("match", "unmatched"),
                notes=raw.get("notes", ""),
                suppliers=[],
            )
            session.add(item)

        session.commit()
        # Re-fetch with items loaded
        session.refresh(rfq)

        # Bind the thread to this RFQ for the creating user
        thread_id = data.get("thread_id")
        if thread_id:
            from includes.dashboard.models import RFQThread
            session.add(RFQThread(
                rfq_number=new_number,
                user_email=user_id,
                thread_id=thread_id,
            ))
            session.commit()

        result = _rfq_to_dict(rfq)
        logger.info(f"Created {new_number} for {customer} with {len(raw_items)} items")
        return result
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _get_rfq_dict_sync(rfq_number: str) -> dict | None:
    """Fetch a single RFQ as a dict, or None if not found."""
    rfq, session = _get_rfq_sync(rfq_number)
    try:
        if not rfq:
            return None
        return _rfq_to_dict(rfq)
    finally:
        session.close()


@_serialized_rfq_write
def _update_item_groups_sync(rfq_number: str, groups_data: dict, user_id: str) -> dict | str:
    """Update item_groups on an RFQ. Returns RFQ dict or error string."""
    rfq, session = _get_rfq_sync(rfq_number)
    try:
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        rfq.item_groups = groups_data
        rfq.updated_at = _now_dt()
        history = list(rfq.history or [])
        n_groups = len(groups_data.get("groups", []))
        history.append({
            "date": _now_iso(),
            "user": user_id,
            "action": f"Updated item groups ({n_groups} groups)",
        })
        rfq.history = history
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _add_items_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Add multiple items to an existing RFQ. Returns RFQ dict or error."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy import func as sa_func

    raw_items = data.get("items", [])
    if not raw_items:
        return "Error: 'items' list is required for add_items."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        max_line = session.query(sa_func.max(RFQItem.line)).filter(
            RFQItem.rfq_id == rfq.id
        ).scalar() or 0

        for idx, raw in enumerate(raw_items, start=max_line + 1):
            try:
                department_id = _resolve_department(raw)
            except ValueError as e:
                return (
                    f"Error: item {raw.get('input_description') or idx} has "
                    f"invalid department — {e}"
                )
            item = RFQItem(
                rfq_id=rfq.id,
                line=idx,
                input_description=raw.get("input_description", ""),
                input_code=raw.get("input_code", ""),
                part_number=raw.get("part_number") or raw.get("input_code") or None,
                brand=raw.get("brand"),
                department_id=department_id,
                product_id=raw.get("product_id"),
                quantity=raw.get("quantity"),
                uom=raw.get("uom", "ea"),
                match=raw.get("match", "unmatched"),
                notes=raw.get("notes", ""),
                suppliers=[],
            )
            session.add(item)

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now,
            "user": user_id,
            "action": f"Added {len(raw_items)} items (lines {max_line + 1}-{max_line + len(raw_items)})",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        rfq.item_groups = None  # invalidate grouping — items have changed
        session.commit()
        session.refresh(rfq)
        result = _rfq_to_dict(rfq)
        logger.info(f"Added {len(raw_items)} items to {rfq_number}")
        return result
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _list_rfqs_sync(assigned_to: str | None = None, status: str | None = None) -> list[dict]:
    """List RFQs with optional filters, ordered by created_date desc."""
    from includes.dashboard.models import RFQ
    session = _get_session()
    try:
        q = session.query(RFQ)
        if assigned_to:
            q = q.filter(RFQ.assigned_to == assigned_to)
        if status:
            q = q.filter(RFQ.status == status)
        q = q.order_by(RFQ.created_date.desc())
        return [_rfq_to_dict(r) for r in q.limit(200).all()]
    finally:
        session.close()


@_serialized_rfq_write
def _update_rfq_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update RFQ header fields. Returns dict or error string."""
    from includes.dashboard.models import RFQ, Opportunity
    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        updatable = [
            "customer", "customer_id", "customer_contact", "reference", "title",
            "notes", "netsuite_opportunity", "opportunity_id", "hubspot_deal", "assigned_to",
        ]
        changes = []
        for key in updatable:
            if key in data:
                val = data[key]
                # customer_id / opportunity_id: accept UUID string, empty clears it
                if key in ("customer_id", "opportunity_id"):
                    if val and isinstance(val, str) and val.strip():
                        from uuid import UUID
                        setattr(rfq, key, UUID(val.strip()))
                        changes.append(key)
                    elif val == "" or val is None:
                        setattr(rfq, key, None)
                        changes.append(key)
                else:
                    setattr(rfq, key, val)
                    changes.append(key)

        # Quote brand — only settable via a reference that resolves in the
        # brands table. Accepted references, in order of preference:
        #   quote_brand_id: the brand's NetSuite ID or its internal UUID
        #   quote_brand:    the exact brand name (case-insensitive match)
        # The name snapshot is refreshed from the DB on every save. Unknown
        # brands cannot be set.
        if "quote_brand_id" in data or "quote_brand" in data:
            from uuid import UUID as _UUID
            from sqlalchemy import func as sa_func
            from includes.dashboard.models import Brand
            brand_id = data.get("quote_brand_id")
            brand_name = data.get("quote_brand")

            # Clear when both are explicitly empty
            if (brand_id in ("", None)) and (brand_name in ("", None)):
                rfq.quote_brand_id = None
                rfq.quote_brand = None
                changes.append("quote_brand")
            else:
                brand = None
                if brand_id and isinstance(brand_id, str) and brand_id.strip():
                    val = brand_id.strip()
                    try:
                        brand = session.get(Brand, _UUID(val))
                    except ValueError:
                        # Not a UUID — treat it as a NetSuite ID
                        brand = (
                            session.query(Brand)
                            .filter(Brand.netsuite_id == val)
                            .first()
                        )
                    if not brand:
                        return f"Error: quote brand '{val}' not found in the brands database."
                elif brand_name and isinstance(brand_name, str) and brand_name.strip():
                    q = brand_name.strip()
                    brand = (
                        session.query(Brand)
                        .filter(sa_func.lower(Brand.name) == q.lower())
                        .filter(Brand.duplicate_of.is_(None))
                        .order_by(Brand.name)
                        .first()
                    )
                    if not brand:
                        return (
                            f"Error: quote brand '{q}' not found in the brands "
                            f"database (an exact match is required)."
                        )
                else:
                    return (
                        "Error: invalid quote brand input — pass quote_brand_id "
                        "(NetSuite ID or UUID) or quote_brand (exact name)."
                    )

                rfq.quote_brand_id = brand.id
                rfq.quote_brand = brand.name
                changes.append("quote_brand")

        if not changes:
            return f"Error: provide at least one of {', '.join(updatable)} to update."

        # If netsuite_opportunity was linked, sync RFQ status to match the Opportunity
        if "netsuite_opportunity" in changes:
            opp = session.query(Opportunity).filter(
                Opportunity.opportunity_number == data["netsuite_opportunity"]
            ).first()
            if opp and opp.status:
                _OPP_TO_RFQ = {"A": "in_progress", "B": "issued_quote", "C": "closed_won", "D": "closed_lost"}
                new_status = _OPP_TO_RFQ.get(opp.status)
                if new_status and rfq.status != new_status:
                    rfq.status = new_status
                    changes.append(f"status→{new_status}")

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Updated RFQ: {', '.join(changes)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)

        # If title changed and RFQ has a linked NetSuite opportunity,
        # sync the title to NetSuite in a background thread.
        if "title" in changes and rfq.opportunity_id:
            opp = session.query(Opportunity).get(rfq.opportunity_id)
            if opp and opp.netsuite_id:
                import threading
                ns_id = opp.netsuite_id
                new_title = rfq.title or ""
                threading.Thread(
                    target=lambda: _sync_title_to_netsuite(ns_id, new_title),
                    daemon=True,
                    name=f"ns-title-sync-{rfq.rfq_number}",
                ).start()

        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _sync_title_to_netsuite(netsuite_id: str, title: str) -> None:
    """Update the title of a NetSuite Opportunity — runs in a background thread."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        from includes.netsuite.records.opportunity import update_opportunity_title
        update_opportunity_title(netsuite_id, title)
    except Exception:
        logger.exception("Background title sync failed for NS opportunity %s", netsuite_id)


def _update_item_core(session, rfq, line_item, data: dict, user_id: str):
    """Apply updates to a single RFQ line item in-place.

    Does NOT commit, append history, clear item_groups, or update
    timestamps. The caller handles rfq-level side effects.

    Returns (changes, line_num, reset_pipeline) where:
      - changes: list of field names that were modified
      - line_num: the line number (int)
      - reset_pipeline: True if identifying fields changed and
        pipeline_stage should be reset to 'unprocessed'
    """
    changes = []
    # Department is validated against the canonical enum before storing.
    # Accepts department_id (NetSuite string ID) or department (exact
    # case-insensitive label). Empty clears it (departments are editable at
    # any time). Unknown IDs/labels raise — storing something not in the
    # enum would silently break classification and the NetSuite push later.
    if "department_id" in data or "department" in data:
        line_item.department_id = _resolve_department(data)
        changes.append("department_id")

    updatable = [
        "input_description", "input_code", "part_number", "brand",
        "product_id", "quantity", "uom", "match", "notes",
        "sale_price",
    ]
    _no_clear = {"product_id", "match"}
    for key in updatable:
        if key in data:
            new_val = data[key]
            if key in _no_clear and not new_val and getattr(line_item, key, None):
                continue
            setattr(line_item, key, new_val)
            changes.append(key)

    reset_pipeline = False
    _identifying = {"input_description", "input_code", "part_number", "brand"}
    if _identifying & set(data.keys()) and "match" not in data:
        line_item.match = "unmatched"
        line_item.product_id = None
        changes.extend(["match", "product_id"])
        reset_pipeline = True

    return changes, line_item.line, reset_pipeline


@_serialized_rfq_write
def _update_item_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update a single RFQ line item. Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem
    line_num = _get_line(data)
    if line_num is None:
        return "Error: 'line' is required in data for update_item."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        line_item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not line_item:
            return f"Error: line {line_num} not found in {rfq_number}."

        try:
            changes, _line_num, reset_pipeline = _update_item_core(
                session, rfq, line_item, data, user_id,
            )
        except ValueError as e:
            # Validation raises before any mutation, so nothing to roll back —
            # the caller's session.close() discards the open transaction.
            return f"Error: {e}"

        # Rfq-level side effects
        if changes:
            rfq.item_groups = None
        if reset_pipeline and rfq.pipeline_stage not in ("unprocessed",):
            rfq.pipeline_stage = "unprocessed"

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Updated line {line_num}: {', '.join(changes)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _update_items_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update multiple RFQ line items in one transaction.

    data["items"] is a list of per-item update dicts, each containing a
    'line' key plus the fields to update.

    Returns full RFQ dict or error string.
    """
    from includes.dashboard.models import RFQ, RFQItem

    items_data = data.get("items", [])
    if not items_data:
        return "Error: 'items' list is required for update_items_bulk."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        results = []
        any_changes = False
        any_reset = False

        for item_data in items_data:
            line_num = _get_line(item_data)
            if line_num is None:
                results.append(f"Skipped: missing line in {item_data}")
                continue

            line_item = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
            ).first()
            if not line_item:
                results.append(f"Skipped: line {line_num} not found")
                continue

            try:
                changes, ln, reset_pipeline = _update_item_core(
                    session, rfq, line_item, item_data, user_id,
                )
            except ValueError as e:
                results.append(f"Skipped line {line_num}: {e}")
                continue
            if changes:
                any_changes = True
                results.append(f"Line {ln}: {', '.join(changes)}")
            if reset_pipeline:
                any_reset = True

        if not any_changes:
            return f"No changes applied to {rfq_number}. " + "; ".join(results)

        # Rfq-level side effects
        if any_changes:
            rfq.item_groups = None
        if any_reset and rfq.pipeline_stage not in ("unprocessed",):
            rfq.pipeline_stage = "unprocessed"

        updated_count = sum(1 for r in results if r.startswith("Line "))
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Bulk updated {updated_count} items. " + "; ".join(results),
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _delete_item_sync(rfq_number: str, line_num: int, user_id: str) -> dict | str:
    """Delete a single RFQ line item and renumber remaining items.
    Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        line_item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not line_item:
            return f"Error: line {line_num} not found in {rfq_number}."

        desc = line_item.input_description or line_item.part_number or f"line {line_num}"
        session.delete(line_item)
        session.flush()  # commit delete before renumbering to avoid unique constraint

        # Renumber remaining items: shift to negative first to avoid unique
        # constraint violations (PostgreSQL checks per-row during UPDATE,
        # so line 7→6 can collide with existing line 6 if processed first).
        from sqlalchemy import text
        session.execute(
            text("UPDATE rfq_items SET line = -(line - 1) WHERE rfq_id = :rfq_id AND line > :line"),
            {"rfq_id": rfq.id, "line": line_num},
        )
        session.execute(
            text("UPDATE rfq_items SET line = -line WHERE rfq_id = :rfq_id AND line < 0"),
            {"rfq_id": rfq.id},
        )

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Deleted item {line_num}: {desc}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _delete_items_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Delete multiple RFQ line items in one transaction.

    data["lines"] can be:
      - "all" — delete every item
      - a list of line numbers: [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

    Lines are processed in descending order so renumbering doesn't
    affect not-yet-deleted items. Returns the updated RFQ dict or
    an error string.
    """
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy import text

    lines_raw = data.get("lines")
    if lines_raw is None:
        return "Error: 'lines' is required. Use a list of line numbers or 'all'."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        if lines_raw == "all":
            count = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).count()
            if count == 0:
                return f"Error: RFQ '{rfq_number}' has no items to delete."
            session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).delete()
            now = _now_iso()
            history = list(rfq.history or [])
            history.append({"date": now, "user": user_id, "action": f"Deleted all {count} items"})
            rfq.history = history
            rfq.updated_at = _now_dt()
            session.commit()
            session.refresh(rfq)
            return _rfq_to_dict(rfq)

        if not isinstance(lines_raw, list):
            return "Error: 'lines' must be a list of line numbers or 'all'."

        # Deduplicate and sort descending
        line_numbers = sorted(set(int(l) for l in lines_raw), reverse=True)
        if not line_numbers:
            return "Error: no valid line numbers provided."

        deleted = []
        for line_num in line_numbers:
            line_item = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
            ).first()
            if not line_item:
                continue

            desc = line_item.input_description or line_item.part_number or f"line {line_num}"
            session.delete(line_item)
            session.flush()

            # Renumber: shift items above the deleted line down by 1
            session.execute(
                text("UPDATE rfq_items SET line = -(line - 1) WHERE rfq_id = :rfq_id AND line > :line"),
                {"rfq_id": rfq.id, "line": line_num},
            )
            session.execute(
                text("UPDATE rfq_items SET line = -line WHERE rfq_id = :rfq_id AND line < 0"),
                {"rfq_id": rfq.id},
            )

            deleted.append(f"line {line_num}: {desc}")

        if not deleted:
            return f"Error: none of the specified lines were found in {rfq_number}."

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Deleted {len(deleted)} items: {', '.join(deleted)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _add_suppliers_to_line_core(session, rfq, line_item, data):
    """Process and merge suppliers into a single line item in-place.

    Handles: validation, DB matching, product auto-resolve, pricing
    enrichment, dedup/merge. Does NOT commit, append history, update
    timestamps, or auto-progress status.

    Returns (added_names, updated_names, skipped_names, error) where:
      - added_names: list of newly added supplier names
      - updated_names: list of existing supplier names that were updated
      - skipped_names: list of rejected supplier names/strings
      - error: None on success, or an error string if pre-conditions fail
        (e.g. no suppliers provided, all names blank). When error is set,
        all three lists are empty.
    """
    from includes.tools.quote_tools import _match_suppliers_to_db, _enrich_supplier_pricing

    suppliers_list = data.get("suppliers", [])
    if not suppliers_list and data.get("name"):
        suppliers_list = [data]
    if not suppliers_list:
        return [], [], [], "Error: 'name' or 'suppliers' list is required for add_supplier."

    _bad_names = {"unknown", ""}
    skipped_names = []
    valid_suppliers = []
    for sup in suppliers_list:
        # Normalize: agent sends 'currency', we store as 'cost_currency'
        if sup.get("currency") and not sup.get("cost_currency"):
            sup["cost_currency"] = sup["currency"]
        name = (sup.get("name") or "").strip()
        has_db_link = bool(sup.get("supplier_id"))
        if not has_db_link and name.lower() in _bad_names:
            skipped_names.append(name or "Unknown")
        else:
            valid_suppliers.append(sup)

    if skipped_names and not valid_suppliers:
        msg = (
            f"REJECTED: All {len(skipped_names)} supplier(s) have blank or unknown names. "
            f"Please provide actual supplier names."
        )
        return [], [], skipped_names, None  # not really an error, just a rejection

    # Split: suppliers with a DB id (no matching needed) vs those that need lookup
    db_linked = [s for s in valid_suppliers if s.get("supplier_id")]
    needs_match = [s for s in valid_suppliers if not s.get("supplier_id")]

    if needs_match:
        _match_suppliers_to_db(
            needs_match,
            product_hint=" ".join(filter(None, [
                line_item.part_number,
                line_item.brand,
                line_item.input_description,
            ])),
        )
        valid_suppliers = db_linked + needs_match
    else:
        valid_suppliers = db_linked

    # Auto-resolve product_id if the item has a part_number
    product_id = line_item.product_id
    if line_item.part_number:
        from includes.dashboard.models import Product as ProductModel

        if product_id:
            existing = session.query(ProductModel).filter(
                ProductModel.id == product_id
            ).first()
            if not existing or normalize_part_number(existing.part_number or "") != normalize_part_number(line_item.part_number or ""):
                logger.warning(
                    f"[auto-resolve] product_id={product_id} (part={existing.part_number if existing else 'N/A'}) "
                    f"does not match line part_number={line_item.part_number} — re-resolving"
                )
                product_id = None
                line_item.product_id = None

        if not product_id:
            from sqlalchemy import func as sa_func
            norm_pn = normalize_part_number(line_item.part_number or "")
            prod = session.query(ProductModel).filter(
                sa_func.regexp_replace(ProductModel.part_number, '[^a-zA-Z0-9]', '', 'g').ilike(norm_pn)
            ).first()
            if prod:
                line_item.product_id = prod.id
                product_id = prod.id
                logger.info(f"[auto-resolve] Set product_id={prod.id} (part={prod.part_number}) for part_number={line_item.part_number}")

    # Enrich with historical pricing from Transaction table
    _enrich_supplier_pricing(valid_suppliers, str(product_id) if product_id else None)

    current_suppliers = list(line_item.suppliers or [])
    existing_by_name = {s["name"].lower(): s for s in current_suppliers}

    added_names = []
    updated_names = []
    for sup in valid_suppliers:
        name = sup.get("name", "Unknown")
        existing = existing_by_name.get(name.lower())
        if existing:
            for key in ["supplier_id", "contacts", "price", "price_type",
                        "lead_time", "notes", "purchase_ref",
                        "cost_price", "cost_price_aud", "sale_price",
                        "cost_currency",
                        "price_date", "price_doc", "price_doc_type",
                        "transaction_count",
                        "quote_status", "quote_cost", "quote_currency", "quote_leadtime",
                        "quote_part_number"]:
                val = sup.get(key)
                if val is not None and val != "" and val != []:
                    existing[key] = val
            new_status = sup.get("status", "candidate")
            if new_status != "candidate" or existing.get("status") in ("candidate", "dropped"):
                existing["status"] = new_status
            updated_names.append(name)
        else:
            supplier_entry = {
                "supplier_id": sup.get("supplier_id"),
                "name": name,
                "contacts": sup.get("contacts", []),
                "status": sup.get("status", "candidate"),
                "price": sup.get("price"),
                "price_type": sup.get("price_type"),
                "lead_time": sup.get("lead_time"),
                "notes": sup.get("notes", ""),
                "purchase_ref": sup.get("purchase_ref"),
                "cost_price": sup.get("cost_price"),
                "cost_price_aud": sup.get("cost_price_aud"),
                "sale_price": sup.get("sale_price"),
                "cost_currency": sup.get("cost_currency"),
                "price_date": sup.get("price_date"),
                "price_doc": sup.get("price_doc"),
                "price_doc_type": sup.get("price_doc_type"),
                "transaction_count": sup.get("transaction_count"),
                "quote_status": sup.get("quote_status"),
                "quote_cost": sup.get("quote_cost"),
                "quote_currency": sup.get("quote_currency"),
                "quote_leadtime": sup.get("quote_leadtime"),
                "quote_part_number": sup.get("quote_part_number"),
            }
            current_suppliers.append(supplier_entry)
            existing_by_name[name.lower()] = supplier_entry
            added_names.append(name)

    # JSONB mutation — assign a new list and flag modified
    from sqlalchemy.orm.attributes import flag_modified
    line_item.suppliers = current_suppliers
    flag_modified(line_item, "suppliers")

    return added_names, updated_names, skipped_names, None


@_serialized_rfq_write
def _add_supplier_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Add supplier(s) to a line item. Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem

    line_num = _get_line(data)
    if line_num is None:
        return "Error: 'line' is required for add_supplier."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        line_item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not line_item:
            return f"Error: line {line_num} not found in {rfq_number}."

        added, updated, skipped, error = _add_suppliers_to_line_core(
            session, rfq, line_item, data,
        )
        if error:
            return error

        # Build history action
        action_parts = []
        if added:
            action_parts.append(f"Added {len(added)} supplier(s) to line {line_num}: {', '.join(added)}")
        if updated:
            action_parts.append(f"Updated {len(updated)} existing supplier(s) on line {line_num}: {', '.join(updated)}")
        if skipped:
            action_parts.append(
                f"REJECTED {len(skipped)} supplier(s) — no contact URL provided: {', '.join(skipped)}. "
                f"Retry with contacts=[{{\"url\": \"https://...\"}}] for each."
            )

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": " | ".join(action_parts) or f"No changes to suppliers on line {line_num}",
        })
        if added and rfq.status == "draft":
            rfq.status = "in_progress"
            history.append({"date": now, "user": "system", "action": "Status auto-changed to in_progress (suppliers added)"})

        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _add_suppliers_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Add suppliers to multiple RFQ line items in one transaction.

    data["entries"] is a flat list of per-line supplier entries:
        [{"line": 1, "name": "Acme Corp", ...}, {"line": 3, "name": "WidgetCo", ...}]

    Entries for the same line are grouped together by the backend.

    Returns full RFQ dict or error string. Cap: 200 entries.
    """
    from includes.dashboard.models import RFQ, RFQItem

    entries = data.get("entries", [])
    if not entries:
        return "Error: 'entries' list is required for add_suppliers_bulk."

    if len(entries) > 200:
        return f"Error: too many entries ({len(entries)}). Maximum is 200 per call."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        # Group entries by line number
        from collections import defaultdict
        by_line = defaultdict(list)
        for entry in entries:
            line_num = _get_line(entry)
            if line_num is None:
                continue
            by_line[line_num].append(entry)

        # Collect results per line (one supplier-processing call per unique line)
        line_results = {}  # line_num -> (added, updated, skipped, error)
        for line_num, line_entries in by_line.items():
            line_item = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
            ).first()
            if not line_item:
                line_results[line_num] = ([], [], [f"line {line_num} not found"], None)
                continue
            added, updated, skipped, error = _add_suppliers_to_line_core(
                session, rfq, line_item, {"suppliers": line_entries},
            )
            line_results[line_num] = (added, updated, skipped, error)

        # Build summary
        total_added = 0
        total_updated = 0
        result_parts = []
        for line_num in sorted(line_results):
            added, updated, skipped, error = line_results[line_num]
            if error:
                result_parts.append(f"Line {line_num}: {error}")
                continue
            if added:
                total_added += len(added)
                result_parts.append(f"Line {line_num} (+{len(added)} suppliers: {', '.join(added)})")
            if updated:
                total_updated += len(updated)
                result_parts.append(f"Line {line_num} (updated {len(updated)}: {', '.join(updated)})")
            if skipped:
                result_parts.append(f"Line {line_num} (rejected {len(skipped)}: {', '.join(skipped)})")

        if not result_parts:
            return f"No suppliers added to {rfq_number}."

        # Auto-progress status if suppliers were added
        if total_added and rfq.status == "draft":
            rfq.status = "in_progress"

        now = _now_iso()
        history = list(rfq.history or [])
        summary = (
            f"Bulk added {total_added} supplier(s) across {len(by_line)} lines, "
            f"updated {total_updated}. "
        ) + "; ".join(result_parts)
        history.append({"date": now, "user": user_id, "action": summary})
        if total_added and rfq.status == "in_progress" and any(
            h.get("action", "").startswith("Status auto-changed") for h in history[-3:]
        ):
            pass  # avoid duplicate auto-progress entries
        elif total_added and rfq.status == "in_progress":
            history.append({"date": now, "user": "system", "action": "Status auto-changed to in_progress (suppliers added)"})

        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _select_quote_core(session, rfq, line_item, data):
    """Select/deselect a supplier's quote on a single line item in-place.

    Does NOT commit, append history, or update timestamps.
    Returns (action_desc, selected_name) or (error_str, None) on error.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from decimal import Decimal, InvalidOperation

    name = data.get("name")
    if not name:
        return "Error: 'name' is required for select_quote.", None

    suppliers = list(line_item.suppliers or [])
    target = None
    for s in suppliers:
        if (s.get("name") or "").lower() == name.lower():
            target = s
            break
    if not target:
        return f"Error: supplier '{name}' not found on line {line_item.line}.", None

    if target.get("quote_status") == "selected":
        target["quote_status"] = "quoted"
        line_item.cost_price = None
        action = f"Deselected '{name}' on line {line_item.line}"
    else:
        for s in suppliers:
            if s.get("quote_status") == "selected":
                s["quote_status"] = "quoted"
        target["quote_status"] = "selected"
        qc = target.get("quote_cost")
        if qc is not None:
            try:
                line_item.cost_price = Decimal(str(qc))
            except (InvalidOperation, ValueError):
                pass
        # Copy supplier's part number if RFQ item doesn't have a real one yet
        if _is_empty_part_number(line_item.part_number):
            supplier_pn = target.get("quote_part_number")
            if supplier_pn:
                line_item.part_number = str(supplier_pn)
        action = f"Selected '{name}' on line {line_item.line}"

    line_item.suppliers = suppliers
    flag_modified(line_item, "suppliers")
    return action, name


@_serialized_rfq_write
def _select_quote_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Mark a supplier's quote as selected on a line item.

    Deselects any previous selection on that item. Copies the selected
    quote_cost to the item's cost_price.
    """
    from includes.dashboard.models import RFQ, RFQItem

    line_num = _get_line(data)
    name = data.get("name")
    if line_num is None or not name:
        return "Error: 'line' and 'name' are required for select_quote."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        line_item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not line_item:
            return f"Error: line {line_num} not found in {rfq_number}."

        action_desc, _ = _select_quote_core(session, rfq, line_item, data)
        if action_desc.startswith("Error:"):
            return action_desc

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": action_desc})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _select_quotes_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Select suppliers across multiple RFQ lines in one transaction.

    data["selections"] is a list: [{"line": 1, "name": "Acme Corp"}, ...]

    Returns full RFQ dict or error string.
    """
    from includes.dashboard.models import RFQ, RFQItem

    selections = data.get("selections", [])
    if not selections:
        return "Error: 'selections' list is required for select_quotes_bulk."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        results = []
        for sel in selections:
            line_num = _get_line(sel)
            name = sel.get("name")
            if line_num is None or not name:
                results.append(f"Skipped: missing line or name in {sel}")
                continue

            line_item = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
            ).first()
            if not line_item:
                results.append(f"Skipped: line {line_num} not found")
                continue

            action_desc, _ = _select_quote_core(session, rfq, line_item, sel)
            results.append(action_desc)

        selected_count = sum(1 for r in results if r.startswith("Selected"))
        deselected_count = sum(1 for r in results if r.startswith("Deselected"))
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Bulk select: {selected_count} selected, {deselected_count} deselected. " + "; ".join(results),
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _decline_quote_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Mark a supplier's quote as declined and clear its price."""
    data = dict(data)
    data["quote_status"] = "declined"
    data["quote_cost"] = None
    return _update_supplier_sync(rfq_number, data, user_id)


@_serialized_rfq_write
def _set_supplier_meta_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Write supplier-level metadata (shipping, notes, terms) to rfqs.supplier_meta."""
    from includes.dashboard.models import RFQ

    name = data.get("name")
    if not name:
        return "Error: 'name' is required for set_supplier_meta."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        meta = dict(rfq.supplier_meta or {})
        entry = dict(meta.get(name, {}))
        for key in ("shipping_cost", "shipping_currency", "notes", "terms"):
            if key in data:
                val = data[key]
                entry[key] = val if val is not None and val != "" else None
        meta[name] = entry
        rfq.supplier_meta = meta

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Updated supplier meta for '{name}'",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _update_supplier_core(session, rfq, line_item, data):
    """Update fields on a supplier within a line item in-place.

    Does NOT commit, append history, or update timestamps.
    Returns (changes, supplier_name) where changes is a list of field names
    that were modified; or (None, error_str) on error.
    """
    from sqlalchemy.orm.attributes import flag_modified

    name = data.get("name")
    if not name:
        return None, "Error: 'name' is required for update_supplier."

    current_suppliers = list(line_item.suppliers or [])
    supplier = next((s for s in current_suppliers if s["name"] == name), None)
    if not supplier:
        return None, f"Error: supplier '{name}' not found on line {line_item.line}."

    # Normalize: agent sends 'currency', we store as 'cost_currency'
    if "currency" in data and "cost_currency" not in data:
        data["cost_currency"] = data["currency"]

    updatable = [
        "status", "price", "price_type", "cost_currency",
        "lead_time", "notes", "contacts", "purchase_ref",
        "quote_status", "quote_cost", "quote_currency", "quote_leadtime",
        "quote_part_number",
    ]
    changes = []
    for key in updatable:
        if key in data:
            supplier[key] = data[key]
            changes.append(key)

    line_item.suppliers = current_suppliers
    flag_modified(line_item, "suppliers")
    return changes, name


@_serialized_rfq_write
def _update_supplier_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update a supplier on a line item. Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem

    line_num = _get_line(data)
    name = data.get("name")
    if line_num is None or not name:
        return "Error: 'line' and 'name' are required for update_supplier."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        line_item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not line_item:
            return f"Error: line {line_num} not found in {rfq_number}."

        changes, supplier_name = _update_supplier_core(session, rfq, line_item, data)
        if changes is None:
            return supplier_name  # error string

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Updated supplier '{supplier_name}' on line {line_num}: {', '.join(changes)}",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _update_quotes_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update quotation fields across multiple RFQ lines in one transaction.

    data["quotes"] is a list: [{"line": 1, "name": "Acme Corp",
    "quote_cost": 45.50, "quote_status": "quoted", ...}, ...]

    Returns full RFQ dict or error string.
    """
    from includes.dashboard.models import RFQ, RFQItem

    quotes = data.get("quotes", [])
    if not quotes:
        return "Error: 'quotes' list is required for update_quotes_bulk."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        results = []
        for q in quotes:
            line_num = _get_line(q)
            name = q.get("name")
            if line_num is None or not name:
                results.append(f"Skipped: missing line or name in {q}")
                continue

            line_item = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
            ).first()
            if not line_item:
                results.append(f"Skipped: line {line_num} not found")
                continue

            changes, sname = _update_supplier_core(session, rfq, line_item, q)
            if changes is None:
                results.append(f"Line {line_num}: {sname}")
            else:
                results.append(f"Line {line_num} '{sname}': {', '.join(changes)}")

        updated_count = sum(1 for r in results if "': " in r)
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Bulk updated {updated_count} quotes. " + "; ".join(results),
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _clear_suppliers_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Clear suppliers from line item(s). Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem
    line_num = data.get("line")  # optional — None means all lines

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        items_q = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id)
        if line_num is not None:
            items_q = items_q.filter(RFQItem.line == line_num)

        cleared = []
        for item in items_q.all():
            count = len(item.suppliers or [])
            if count:
                item.suppliers = []
                cleared.append(f"line {item.line} ({count})")

        if not cleared:
            scope = f"line {line_num}" if line_num else "any line"
            return f"No suppliers to clear on {scope}."

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Cleared suppliers from {', '.join(cleared)}",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        # Reset pipeline stage when clearing all suppliers
        if line_num is None:
            rfq.pipeline_stage = "unprocessed"
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _assign_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ
    assigned_to = data.get("assigned_to")
    if not assigned_to:
        return "Error: 'assigned_to' is required for assign."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        rfq.assigned_to = assigned_to
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Assigned to {assigned_to}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _update_status_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ
    new_status = data.get("status")
    valid = {"draft", "in_progress", "issued_quote", "closed_won", "closed_lost"}
    if new_status not in valid:
        return f"Error: status must be one of {', '.join(sorted(valid))}."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        rfq.status = new_status
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Status changed to {new_status}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _add_note_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ
    note = data.get("note", "")
    if not note:
        return "Error: 'note' is required for add_note."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        existing = rfq.notes or ""
        rfq.notes = f"{existing}\n{note}".strip() if existing else note
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Added note: {note[:80]}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _link_external_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ, Opportunity
    linked = []
    if "netsuite_opportunity" not in data and "hubspot_deal" not in data:
        return "Error: provide netsuite_opportunity and/or hubspot_deal."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        if "netsuite_opportunity" in data:
            rfq.netsuite_opportunity = data["netsuite_opportunity"]
            linked.append(f"NetSuite: {data['netsuite_opportunity']}")
            # Immediately sync RFQ status to match the Opportunity
            opp = session.query(Opportunity).filter(
                Opportunity.opportunity_number == data["netsuite_opportunity"]
            ).first()
            if opp and opp.status:
                _OPP_TO_RFQ = {"A": "in_progress", "B": "issued_quote", "C": "closed_won", "D": "closed_lost"}
                new_status = _OPP_TO_RFQ.get(opp.status)
                if new_status and rfq.status != new_status:
                    old_status = rfq.status
                    rfq.status = new_status
                    linked.append(f"status {old_status}→{new_status}")
        if "hubspot_deal" in data:
            rfq.hubspot_deal = data["hubspot_deal"]
            linked.append(f"HubSpot: {data['hubspot_deal']}")
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Linked {', '.join(linked)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@_serialized_rfq_write
def _create_opportunity_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Create a NetSuite Opportunity for the RFQ and link it locally.

    Returns the updated RFQ dict on success, or an error string.
    """
    from includes.netsuite.records.opportunity import create_and_link_opportunity

    result = create_and_link_opportunity(rfq_number)
    if not result.success:
        return f"Error: {result.error}"

    # Re-fetch the RFQ to return the updated state
    session = _get_session()
    try:
        from includes.dashboard.models import RFQ
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found after opportunity creation"

        # Append to history
        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now,
            "user": user_id,
            "action": f"Created NetSuite Opportunity {result.tran_id}",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


# =============================================================================
# Shared orchestration helpers — used by both tools (quote_tools.py) and
# action callbacks (rfq_actions.py).  All are synchronous, run via
# asyncio.to_thread.  No Chainlit dependencies.
# =============================================================================

@_serialized_rfq_write
def _set_quote_brand_from_items_sync(rfq_number: str, user_id: str) -> str | None:
    """Deterministically set the RFQ's quote brand from its item brands.

    Called by the classify step (Step 2 of the quote-brand feature). Counts
    non-empty item brands; when a single brand holds a strict majority AND
    matches a brands-table row exactly (case-insensitive), sets
    quote_brand_id + quote_brand and appends a history entry. Ties, and
    majority brands missing from the brands database, are left for a human.
    Never overwrites an existing quote brand.

    Returns a short status string, or None when nothing changed.
    """
    from sqlalchemy import func as sa_func
    from includes.dashboard.models import Brand, RFQ

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."
        if rfq.quote_brand_id:
            return None  # already set — never overwrite

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}
        for item in rfq.items or []:
            brand = (item.brand or "").strip()
            if not brand or brand.lower() in ("other", "n/a", "na", "none", "unknown"):
                continue
            key = brand.lower()
            counts[key] = counts.get(key, 0) + 1
            display_names.setdefault(key, brand)

        if not counts:
            return None  # no item brands to infer from

        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top_key, top_count = ranked[0]
        total = sum(counts.values())
        tied = [k for k, c in ranked if c == top_count]
        if len(tied) > 1:
            names = ", ".join(display_names[k] for k in tied)
            return (
                f"Quote brand not auto-set — item brands tied ({names}); "
                f"human decision needed."
            )

        brand_row = (
            session.query(Brand)
            .filter(sa_func.lower(Brand.name) == top_key)
            .filter(Brand.duplicate_of.is_(None))
            .order_by(Brand.name)
            .first()
        )
        if not brand_row:
            return (
                f"Quote brand not auto-set — majority item brand "
                f"'{display_names[top_key]}' is not in the brands database."
            )

        rfq.quote_brand_id = brand_row.id
        rfq.quote_brand = brand_row.name
        rfq.updated_at = _now_dt()
        history = list(rfq.history or [])
        history.append({
            "date": _now_iso(),
            "user": user_id,
            "action": (
                f"Auto-set quote brand to '{brand_row.name}' "
                f"(majority item brand, {top_count}/{total} items)"
            ),
        })
        rfq.history = history
        session.commit()
        session.refresh(rfq)
        return (
            f"Auto-set quote brand to '{brand_row.name}' "
            f"(majority item brand, {top_count}/{total} items)."
        )
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def _set_item_departments_sync(rfq_number: str, user_id: str) -> str | None:
    """Auto-set item departments after classify & validate.

    Precedence, strongest first:
      1. Existing department — never overwrite.
      2. Product match — copy ``products.department_id`` onto the line
         (deterministic, no LLM).
      3. LLM fallback — one batched call for the remaining items with no
         product match. Output is strictly validated against the enum;
         uncertain/unknown assignments are skipped, leaving NULL.

    Returns a short status string, or None when nothing changed.
    """
    import json
    from includes.dashboard.models import Product
    from includes.netsuite.departments import (
        DEPARTMENT_BY_ID, department_prompt_table,
    )

    rfq_dict = _get_rfq_dict_sync(rfq_number)
    if not rfq_dict:
        return f"Error: RFQ '{rfq_number}' not found."
    items = rfq_dict.get("items", [])
    if not items:
        return None

    # ── Pass 1: product matches (no LLM) ──────────────────────────────
    product_lines: dict[int, str] = {}
    for item in items:
        if item.get("department_id") or not item.get("product_id"):
            continue
        product_lines[item["line"]] = item["product_id"]

    product_updates: list[dict] = []
    if product_lines:
        session = _get_session()
        try:
            rows = (
                session.query(Product.id, Product.department_id)
                .filter(Product.id.in_(list(product_lines.values())))
                .all()
            )
        finally:
            session.close()
        dept_by_pid = {str(r.id): r.department_id for r in rows}
        for line, pid in product_lines.items():
            dept_id = dept_by_pid.get(pid)
            if dept_id and dept_id in DEPARTMENT_BY_ID:
                product_updates.append({"line": line, "department_id": dept_id})

    if product_updates:
        result = _update_items_bulk_sync(
            rfq_number, {"items": product_updates}, user_id,
        )
        if isinstance(result, str):
            return f"Error applying product departments: {result}"

    # ── Pass 2: LLM fallback for the rest ─────────────────────────────
    done_lines = {u["line"] for u in product_updates}
    remaining = [
        {
            "line": item["line"],
            "input_description": item.get("input_description", ""),
            "part_number": item.get("part_number"),
            "brand": item.get("brand"),
        }
        for item in items
        if not item.get("department_id") and item["line"] not in done_lines
    ]
    if not remaining:
        if product_updates:
            return (
                f"Departments auto-set: {len(product_updates)} from product "
                f"match."
            )
        return None

    from google import genai as _genai
    from google.genai import types as _types
    from config.settings import Config
    from includes.prompts import load_prompt

    prompt = load_prompt("rfq_item_departments").replace(
        "{{DEPARTMENT_TABLE}}", department_prompt_table(),
    )
    payload = json.dumps({"items": remaining}, indent=2)
    full_prompt = (
        f"{prompt}\n\n---\n\n"
        f"## Items to classify\n\n"
        f"```json\n{payload}\n```\n\n"
        f"Return ONLY the JSON object specified in section 4."
    )

    try:
        client = _genai.Client(http_options={"timeout": 120000})
        response = client.models.generate_content(
            model=Config.get_agent_model("procurement"),
            contents=full_prompt,
            config=_types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        raw_text = (response.text or "").strip()
    except Exception as e:
        return f"Department LLM call failed: {e}"

    cleaned = raw_text
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return f"Failed to parse department LLM response as JSON: {e}"

    assignments = parsed.get("departments", {}) if isinstance(parsed, dict) else {}
    if not isinstance(assignments, dict):
        return "Failed to parse department LLM response: 'departments' must be an object."

    remaining_lines = {item["line"] for item in remaining}
    llm_updates: list[dict] = []
    skipped: list[str] = []
    for line_key, dept_val in assignments.items():
        try:
            line = int(line_key)
        except (TypeError, ValueError):
            skipped.append(str(line_key))
            continue
        if line not in remaining_lines:
            skipped.append(f"line {line} (not in input)")
            continue
        dept_id = str(dept_val).strip()
        if dept_id not in DEPARTMENT_BY_ID:
            skipped.append(f"line {line} (unknown department '{dept_val}')")
            continue
        llm_updates.append({"line": line, "department_id": dept_id})

    if llm_updates:
        result = _update_items_bulk_sync(
            rfq_number, {"items": llm_updates}, user_id,
        )
        if isinstance(result, str):
            return f"Error applying LLM departments: {result}"

    parts = []
    if product_updates:
        parts.append(f"{len(product_updates)} from product match")
    if llm_updates:
        parts.append(f"{len(llm_updates)} by LLM")
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    if not parts:
        return None
    return f"Departments auto-set: {', '.join(parts)}."


def _classify_rfq_items_sync(
    rfq_number: str, user_id: str, search_db: bool = True,
) -> dict:
    """Classify all unmatched items and optionally search product DB.

    Returns a summary dict:
        classified: {specific: [line,...], branded: [...], generic: [...]}
        db_matches: [(line, part_number, brand, product_id), ...]
        to_validate: [item_dict, ...]
        unclassifiable: [item_dict, ...]
    """
    from includes.tools.product_tools import _find_product_by_code

    rfq_dict = _get_rfq_dict_sync(rfq_number)
    if not rfq_dict:
        return {"error": f"RFQ '{rfq_number}' not found."}

    items = rfq_dict.get("items", [])
    unmatched_items = [i for i in items if i.get("match") == "unmatched"]

    classified = {"specific": [], "branded": [], "generic": []}
    to_validate = []
    db_matches = []
    unclassifiable = []

    for item in unmatched_items:
        line = item["line"]
        part_number = (item.get("part_number") or "").strip()
        brand = (item.get("brand") or "").strip()
        desc = (item.get("input_description") or "").strip()

        has_part = bool(part_number)
        has_brand = bool(brand and brand.lower()
                         not in ("other", "n/a", "na", "none", "unknown"))
        has_desc = bool(desc)

        match = None
        if has_part and has_desc:
            match = "specific"
        elif has_brand and has_desc:
            match = "branded"
        elif has_desc:
            match = "generic"

        if match:
            classified[match].append(line)
            if match == "specific":
                to_validate.append(item)
        else:
            unclassifiable.append(item)

    # Bulk-update classification matches (was N individual _update_item_sync calls)
    class_updates = [
        {"line": line, "match": match_cat}
        for match_cat, lines in classified.items()
        for line in lines
    ]
    if class_updates:
        _update_items_bulk_sync(rfq_number, {"items": class_updates}, user_id)

    if search_db and to_validate:
        db_updates = []
        for item in to_validate:
            line = item["line"]
            part_number = item.get("part_number", "")
            brand = item.get("brand", "")
            try:
                product = _find_product_by_code(part_number, brand or None)
            except (KeyError, ValueError, LookupError):
                product = None
            if product:
                db_updates.append({
                    "line": line,
                    "part_number": product["part_number"],
                    "brand": product["brand"],
                    "product_id": product["id"],
                    "match": "specific",
                })
                db_matches.append((
                    line, product["part_number"],
                    product["brand"], product["id"],
                ))
        if db_updates:
            _update_items_bulk_sync(rfq_number, {"items": db_updates}, user_id)

    # Step 2: auto-set the quote brand from item brands (deterministic
    # majority; ties and non-DB brands are left for a human).
    quote_brand_result = _set_quote_brand_from_items_sync(rfq_number, user_id)

    # Step 3: auto-set item departments — product match first, then one
    # batched LLM call for the remainder (strict enum validation).
    department_result = _set_item_departments_sync(rfq_number, user_id)

    return {
        "classified": classified,
        "db_matches": db_matches,
        "to_validate": to_validate,
        "unclassifiable": unclassifiable,
        "quote_brand_result": quote_brand_result,
        "department_result": department_result,
    }


def _group_rfq_items_sync(
    rfq_number: str, specific_items: list, user_id: str,
) -> dict:
    """Group specific items by brand/supply chain using LLM.

    Returns parsed groups dict, or {'error': ...} on failure.
    """
    import json
    from google import genai as _genai
    from google.genai import types as _types
    from config import config
    from includes.prompts import load_prompt

    if len(specific_items) < 2:
        return {"error": f"Need at least 2 specific items, got {len(specific_items)}."}

    grouping_prompt = load_prompt("rfq_item_grouping")
    input_payload = json.dumps({
        "items": specific_items,
        "existing_groups": None,
    }, indent=2)

    full_prompt = (
        f"{grouping_prompt}\n\n---\n\n"
        f"## Your Task\n\n"
        f"Group the following items from **{rfq_number}**.\n\n"
        f"```json\n{input_payload}\n```\n\n"
        f"Return ONLY the JSON output as specified in section 3."
    )

    try:
        client = _genai.Client(http_options={"timeout": 120000})
        response = client.models.generate_content(
            model=config.get_agent_model("procurement"),
            contents=full_prompt,
            config=_types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        raw_text = (response.text or "").strip()
    except Exception as e:
        return {"error": f"LLM call failed: {e}"}

    cleaned = raw_text
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse LLM response as JSON: {e}"}

    _update_item_groups_sync(rfq_number, result, user_id)
    return result


def _apply_validation_results(validated: list, rfq_number: str) -> None:
    """Write validation outcomes back to RFQ items.

    - discrepancy: match='discrepancy' (blocked pending human review)
    - multi_brand: match='specific' — the part is a valid cross-brand
      designation; record the equivalent-manufacturer findings in notes
    """
    from includes.dashboard.models import RFQ, RFQItem

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return
        for v in validated:
            status = v.get("status")
            if status not in ("discrepancy", "multi_brand"):
                continue
            item = (
                session.query(RFQItem)
                .filter(RFQItem.rfq_id == rfq.id, RFQItem.line == v.get("line"))
                .first()
            )
            if not item:
                continue
            findings = v.get("findings", "")
            if status == "discrepancy":
                item.match = "discrepancy"
                item.notes = findings
                if v.get("correct_part_number") and v["correct_part_number"] != item.part_number:
                    item.notes += f" (Correct PN: {v['correct_part_number']})"
            else:  # multi_brand — valid as-is, supplier search can proceed
                item.match = "specific"
                if findings:
                    item.notes = findings
        session.commit()
    except SQLAlchemyError as e:
        logger.warning(f"Failed to update validation results: {e}")
        session.rollback()
    finally:
        session.close()


@_serialized_rfq_write
def _validate_items_sync(
    rfq_number: str, items_to_validate: list, user_id: str,
) -> dict:
    """Validate specific items via web search using a two-step approach.

    Step 1: Search the web with grounding to gather findings about each part.
    Step 2: Format findings into structured JSON with a separate (ungrounded) call.

    This separation ensures grounding does its job (web research) without
    corrupting the structured output (which grounding can truncate).

    Args:
        rfq_number: The RFQ identifier.
        items_to_validate: List of dicts with keys: line, input_description,
                          part_number, brand.
        user_id: Current user ID.

    Returns:
        {
            "validated": [{line, status, findings, correct_part_number}],
            "error": str (only if failed)
        }
    """
    import json
    from google import genai as _genai
    from google.genai import types as _types
    from config.settings import Config

    if not items_to_validate:
        return {"validated": []}

    # Build the items description
    items_text = "\n".join(
        f"- Line {item['line']}: Part# {item.get('part_number', '?')} | "
        f"Brand: {item.get('brand', '?')} | Description: {item.get('input_description', '?')}"
        for item in items_to_validate
    )

    # ------------------------------------------------------------------
    # Step 1: Web research with grounding (free-form text output)
    # ------------------------------------------------------------------
    research_prompt = f"""You are validating part numbers for a purchase order. For each item below, search the web to verify:
1. Is the part number real and active?
2. Does the description match what the manufacturer says?
3. Is the brand correct (if a brand is given)?
4. If the part number is wrong, what is the correct one?

Important: some part numbers are standard industry designations used across
many manufacturers (e.g. belt size codes like 'B82', hydraulic fitting and
bearing codes). These are VALID without any brand — report the equivalent
manufacturers instead of flagging them as wrong.

Items to validate:
{items_text}

For each item, describe what you found. Include the line number, whether the part checks out or has issues, and any corrections needed."""

    try:
        client = _genai.Client(http_options={"timeout": 120000})  # 2 min timeout

        # Call 1: Grounded web search — get research findings as plain text
        response = client.models.generate_content(
            model=Config.DEFAULT_MODEL,
            contents=research_prompt,
            config=_types.GenerateContentConfig(
                tools=[_types.Tool(google_search=_types.GoogleSearch())],
                temperature=0.1,
            ),
        )

        # Extract text from all parts (grounding splits response)
        findings_text = ""
        try:
            if response.candidates:
                parts = response.candidates[0].content.parts or []
                findings_text = "".join(
                    p.text for p in parts
                    if hasattr(p, "text") and p.text
                ).strip()
        except (ValueError, AttributeError, IndexError):
            pass
        if not findings_text:
            try:
                findings_text = (response.text or "").strip()
            except (ValueError, AttributeError):
                pass

        logger.info(f"Validation step 1 (research): {len(findings_text)} chars")

        if not findings_text:
            return {"validated": [], "error": "Empty response from web research step"}

        # ------------------------------------------------------------------
        # Step 2: Format findings into structured JSON (no grounding)
        # ------------------------------------------------------------------
        format_prompt = f"""Based on the research findings below, produce a JSON array summarizing the validation results.

Original items:
{items_text}

Research findings:
{findings_text}

For each item, produce a JSON object with:
- "line": the line number (integer)
- "status": "confirmed" if everything matches, "multi_brand" if the part number is a standard industry designation valid across multiple manufacturers (no single brand to confirm), "discrepancy" if something is wrong, "not_found" if the part number doesn't exist online
- "findings": a brief 1-2 sentence summary of what was found
- "correct_part_number": the correct part number if a typo was found (otherwise same as original)

Return ONLY a valid JSON array, no other text."""

        response2 = client.models.generate_content(
            model=Config.DEFAULT_MODEL,
            contents=format_prompt,
            config=_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )

        raw_text = ""
        try:
            raw_text = (response2.text or "").strip()
        except (ValueError, AttributeError):
            pass
        if not raw_text and response2.candidates:
            parts = response2.candidates[0].content.parts or []
            raw_text = "".join(p.text or "" for p in parts if hasattr(p, "text")).strip()

        logger.info(f"Validation step 2 (format): {len(raw_text)} chars: {raw_text[:200]!r}")

        if not raw_text:
            return {"validated": [], "error": "Empty response from formatting step"}

        # Clean markdown fences if present
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw_text = "\n".join(lines).strip()

        # Extract JSON array from any surrounding text
        json_start = raw_text.find("[")
        json_end = raw_text.rfind("]")
        if json_start != -1 and json_end != -1 and json_end > json_start:
            raw_text = raw_text[json_start:json_end + 1]

        validated = json.loads(raw_text)

        # Write outcomes back to the items (discrepancy / multi_brand)
        _apply_validation_results(validated, rfq_number)

        return {"validated": validated}
    except Exception as e:
        logger.error(f"Validation failed: {e}")
        return {"validated": [], "error": str(e)}


def _web_search_suppliers_sync(
    description: str,
    part_number: str = "",
    brand: str = "",
    existing_suppliers: list[str] | None = None,
    quantity: str = "",
    domestic_only: bool = False,
) -> list[dict]:
    """Search the web for suppliers of a product using Google Search grounding.

    Two-step approach:
      1. RESEARCH — free-form search with grounding (RESEARCH_AGENT_MODEL).
         No JSON constraints — the model freely explores and reports findings.
      2. EXTRACT — SUPERVISOR_MODEL parses the research text into structured JSON.

    Args:
        domestic_only: If True, restrict search to Australian suppliers only.

    Returns a list of supplier dicts ready for _add_supplier_sync:
        [{"name", "country", "currency", "contacts", "status", "price_type", "notes", ...}]
    """
    import json
    from google import genai as _genai
    from google.genai import types as _types
    from config.settings import Config

    existing_str = ""
    if existing_suppliers:
        existing_str = (
            f"\nAlready have these suppliers (do NOT include them): "
            f"{', '.join(existing_suppliers)}"
        )

    product_desc = description
    if part_number:
        product_desc = f"{part_number} - {description}"
    if brand:
        product_desc = f"{brand} {product_desc}"

    geography_instruction = (
        "Search ONLY for suppliers based in Australia. "
        "Focus exclusively on Australian distributors and wholesalers."
        if domestic_only
        else "Search globally for suppliers — do not restrict to any country."
    )

    # =========================================================================
    # STEP 1: Research — free-form search with grounding (no JSON constraint)
    # =========================================================================
    research_prompt = f"""Search the web for companies that sell or distribute this product:

Product: {product_desc}
{f"Part Number: {part_number}" if part_number else ""}
{f"Brand: {brand}" if brand else ""}
{f"Quantity needed: {quantity}" if quantity else ""}
{existing_str}

{geography_instruction}

Find 3–5 suppliers. Prioritise authorised distributors and industrial wholesalers.

For EACH supplier, try to find as much of this information as possible:
- Company name
- Country (2-letter ISO code: AU, US, GB, NZ, etc.)
- Their trading currency (AUD, USD, NZD, GBP, etc.)
- Website URL — this is critical, search for their official site
- Contact email — look for sales@ or info@ addresses
- Contact phone number
- Any visible pricing
- Brief notes on what they supply

Write your findings as a clear, detailed report. List each supplier with all the
information you found. If you couldn't find a particular field, note it as
"not found" rather than omitting it."""

    research_text = ""
    try:
        # Resolve model: RESEARCH_AGENT_MODEL → DEFAULT_MODEL (same as ResearchAgent)
        web_search_model = Config.RESEARCH_AGENT_MODEL or Config.DEFAULT_MODEL
        client = _genai.Client(http_options={"timeout": 120000})
        logger.info(f"Web search research: model={web_search_model}, product={product_desc[:80]}")
        response = client.models.generate_content(
            model=web_search_model,
            contents=research_prompt,
            config=_types.GenerateContentConfig(
                tools=[_types.Tool(google_search=_types.GoogleSearch())],
                temperature=0.3,
            ),
        )
        # --- Detailed diagnostic logging ---
        has_candidates = bool(response.candidates)
        finish_reason = None
        part_details = []
        try:
            if response.candidates:
                finish_reason = str(response.candidates[0].finish_reason) if response.candidates[0].finish_reason else "None"
                parts = response.candidates[0].content.parts or []
                for i, p in enumerate(parts):
                    attrs = [a for a in dir(p) if not a.startswith('_')]
                    has_text = hasattr(p, "text") and bool(p.text)
                    text_preview = (p.text[:100] if has_text else "") if hasattr(p, "text") else ""
                    part_details.append(
                        f"  part[{i}]: attrs={attrs}, has_text={has_text}, "
                        f"text_preview={text_preview!r}"
                    )
                    if has_text:
                        research_text += p.text
                research_text = research_text.strip()
        except Exception as e:
            part_details.append(f"  ERROR extracting parts: {e}")
        logger.info(
            f"Web search research RAW:\n"
            f"  model={web_search_model}\n"
            f"  has_candidates={has_candidates}\n"
            f"  finish_reason={finish_reason}\n"
            f"  text_len={len(research_text)}\n"
            + "\n".join(part_details)
        )
        # Log the full research text (truncated for log safety)
        if research_text:
            logger.info(
                f"Web search research TEXT ({len(research_text)} chars):\n"
                f"{research_text[:2000]}"
            )
        else:
            # Try fallback
            try:
                fallback = (response.text or "").strip()
                logger.info(f"Web search research FALLBACK text: {fallback[:500]!r}")
                research_text = fallback
            except Exception as e2:
                logger.warning(f"Web search fallback also failed: {e2}")
    except Exception as e:
        logger.error(f"Web search research step FAILED: {type(e).__name__}: {e}", exc_info=True)
        return []

    if not research_text:
        logger.warning(f"Web search returned empty response for: {product_desc[:80]}")
        return []

    logger.info(f"Research step produced {len(research_text)} chars for: {product_desc[:80]}")

    # =========================================================================
    # STEP 2: Extract — parse research text into structured JSON
    # =========================================================================
    # NOTE: Use .replace() instead of f-string to avoid crashes if the
    # research text contains { or } characters (common in URLs and JSON).
    _extract_template = """Extract supplier information from the following research report
into a JSON array of supplier objects.

For each supplier mentioned, create an object with these fields:
- name: Company name (required)
- country: 2-letter ISO country code (e.g. AU, US, GB, NZ) — infer from the report context
- currency: Trading currency (e.g. AUD, USD, NZD, GBP) — infer from country if not stated
- website: Website URL (full URL, or empty string if not found)
- email: Contact email (or empty string)
- phone: Contact phone (or empty string)
- price: Numeric price if mentioned (or null)
- price_currency: Currency of the price (or null)
- notes: Brief description of what they supply

Rules:
- Include only suppliers that are clearly identified with a company name.
- Don't fabricate information — if a field wasn't in the report, use empty string or null.
- Infer country/currency from context (e.g. a .com.au domain → AU/AUD).
- The "website" field is important — prefer the full URL if mentioned.

Research report:
---
__RESEARCH_TEXT__
---

Return ONLY the JSON array. No other text.
Example: [{"name": "Example Co", "country": "AU", "currency": "AUD", "website": "https://example.com", "email": "sales@example.com", "phone": "03 9000 1234", "price": null, "price_currency": null, "notes": "Industrial distributor"}]"""

    extract_prompt = _extract_template.replace(
        "__RESEARCH_TEXT__", research_text[:8000]
    )

    try:
        client = _genai.Client(http_options={"timeout": 60000})
        response = client.models.generate_content(
            model=Config.SUPERVISOR_MODEL,
            contents=extract_prompt,
            config=_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=2048,
            ),
        )
        raw_text = (response.text or "").strip()
        logger.info(f"Web search extract: {len(raw_text)} chars from {Config.SUPERVISOR_MODEL}")
    except Exception as e:
        logger.error(f"Web search extract step failed: {e}")
        return []

    # Clean markdown fences
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()

    # Extract JSON array from surrounding text
    json_start = raw_text.find("[")
    json_end = raw_text.rfind("]")
    if json_start != -1 and json_end != -1 and json_end > json_start:
        raw_text = raw_text[json_start:json_end + 1]

    try:
        suppliers_raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Web search JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        return []

    if not isinstance(suppliers_raw, list):
        logger.warning(f"Web search returned non-list: {type(suppliers_raw)}")
        return []

    # =========================================================================
    # Normalize to the format expected by _add_supplier_sync
    # =========================================================================
    results = []
    existing_lower = {n.lower() for n in (existing_suppliers or [])}
    for s in suppliers_raw:
        name = (s.get("name") or "").strip()
        if not name or name.lower() in existing_lower:
            continue
        contacts = []
        contact = {}
        if s.get("website"):
            contact["url"] = s["website"]
        if s.get("email"):
            contact["email"] = s["email"]
        if s.get("phone"):
            contact["phone"] = s["phone"]
        if contact:
            contacts.append(contact)

        entry = {
            "name": name,
            "country": s.get("country", ""),
            "currency": s.get("currency", ""),
            "contacts": contacts,
            "status": "candidate",
            "price_type": "web_search",
            "notes": s.get("notes", ""),
        }
        if s.get("price") and s.get("price_currency"):
            entry["cost_price"] = s["price"]
            entry["cost_currency"] = s["price_currency"]
        elif s.get("price"):
            entry["cost_price"] = s["price"]

        results.append(entry)
        existing_lower.add(name.lower())

    logger.info(f"Web search found {len(results)} suppliers for: {product_desc[:80]}")
    return results


def _find_purchase_suppliers_sync(
    rfq_number: str, user_id: str,
) -> dict:
    """Search purchase history for all specific items and add suppliers.

    Returns {added: int, by_line: {line: [supplier_names]}}.
    """
    from includes.tools.product_tools import _find_purchase_history_for_part

    rfq_dict = _get_rfq_dict_sync(rfq_number)
    if not rfq_dict:
        return {"error": f"RFQ '{rfq_number}' not found."}

    items = rfq_dict.get("items", [])
    specific_items = [i for i in items if i.get("match") == "specific"]

    total_added = 0
    by_line = {}
    skipped_duplicates = []

    for item in specific_items:
        line = item["line"]
        part_number = item.get("part_number", "")
        if not part_number:
            continue

        existing = item.get("suppliers", [])
        existing_names = {s["name"].lower() for s in existing}
        suppliers = []

        try:
            ph_rows = _find_purchase_history_for_part(part_number, 20)
            for row in ph_rows:
                # Known duplicates are surfaced for historical context only —
                # they must never be linked to a new RFQ.
                if row.get("is_duplicate"):
                    if row["name"] not in skipped_duplicates:
                        skipped_duplicates.append(row["name"])
                    continue
                if row["name"].lower() not in existing_names:
                    suppliers.append({
                        "supplier_id": row["supplier_id"],
                        "name": row["name"],
                        "contacts": row["contacts"],
                        "status": "candidate",
                        "price_type": "previous_purchase",
                        "price": row["price"],
                        "purchase_ref": {
                            "doc_number": row["doc_number"],
                            "date": row["date"],
                            "order_count": row["order_count"],
                        },
                    })
                    existing_names.add(row["name"].lower())
        except (SQLAlchemyError, KeyError, ValueError):
            pass

        if suppliers:
            _add_supplier_sync(rfq_number, {"line": line, "suppliers": suppliers}, user_id)
            total_added += len(suppliers)
            by_line[line] = [s["name"] for s in suppliers]

    return {
        "added": total_added,
        "by_line": by_line,
        "skipped_duplicates": skipped_duplicates,
    }


@_serialized_rfq_write
def _find_brand_suppliers_sync(rfq_number: str, user_id: str) -> dict:
    """Find brand-linked suppliers for all items with a brand, add top Tier A to RFQ.

    Looks up each item's brand in the supplier-brand link table via
    _find_brand_suppliers_with_tier(). Auto-adds up to 5 Tier A suppliers
    per line item. Stores the full brand-supplier list on the item's
    brand_suppliers JSON column for reference in the UI modal.

    Args:
        rfq_number: The RFQ identifier (e.g. "RFQ-2026-0042").
        user_id: Current user's identifier.

    Returns:
        {
            "added": int,                              # total Tier A suppliers added
            "by_line": {line_num: [supplier_names]},   # what was added where
        }
    """
    from includes.tools.product_tools import _find_brand_suppliers_with_tier
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    rfq_dict = _get_rfq_dict_sync(rfq_number)
    if not rfq_dict:
        return {"error": f"RFQ '{rfq_number}' not found."}

    items = rfq_dict.get("items", [])
    total_added = 0
    by_line = {}

    session = _get_session()
    try:
        for item in items:
            line = item["line"]
            brand = (item.get("brand") or "").strip()

            # Skip items without a real brand
            if not brand or brand.lower() in ("other", "n/a", "na", "none", "unknown"):
                continue

            # Look up brand-linked suppliers
            try:
                brand_sups = _find_brand_suppliers_with_tier(brand)
            except (SQLAlchemyError, KeyError, ValueError):
                logger.warning(
                    f"Brand supplier lookup failed for line {line} brand={brand}",
                    exc_info=True,
                )
                continue

            if not brand_sups:
                continue

            # Save full brand-supplier list to the item for modal reference
            rfq_obj = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
            if not rfq_obj:
                continue
            item_obj = session.query(RFQItem).filter(
                RFQItem.rfq_id == rfq_obj.id, RFQItem.line == line,
            ).first()
            if item_obj:
                item_obj.brand_suppliers = brand_sups
                flag_modified(item_obj, "brand_suppliers")
            session.commit()

            # Determine which suppliers are already on this line
            existing_suppliers = item.get("suppliers", [])
            existing_names_lower = {s["name"].lower() for s in existing_suppliers if isinstance(s, dict)}

            # Filter to only new suppliers (not already on the line)
            new_brand_sups = [
                s for s in brand_sups
                if s["name"].lower() not in existing_names_lower
            ]

            # Auto-add top 5 suppliers to the line item (same sort order as modal)
            top_suppliers = new_brand_sups[:5]
            if top_suppliers:
                top_entries = [
                    {
                        "supplier_id": s["supplier_id"],
                        "name": s["name"],
                        "contacts": s.get("contacts", []),
                        "status": "candidate",
                        "price_type": "brand_link",
                        "notes": (
                            f"Brand-linked supplier (Tier {s.get('tier', '?')}, "
                            f"{s['transaction_count']} transactions)"
                        ),
                    }
                    for s in top_suppliers
                ]
                _add_supplier_sync(
                    rfq_number,
                    {"line": line, "suppliers": top_entries},
                    user_id,
                )
                total_added += len(top_suppliers)
                by_line[line] = [s["name"] for s in top_suppliers]
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()

    return {"added": total_added, "by_line": by_line}


@_serialized_rfq_write
def _cross_apply_suppliers_sync(rfq_number: str, user_id: str) -> dict:
    """Cross-apply suppliers within item groups so grouped items share suppliers.

    For each group (stored in rfq.item_groups), collects all suppliers found
    on any line in the group, then adds missing ones to peer lines. This
    ensures that if Line 1 has Supplier A and Line 2 has Supplier B, and
    both lines are in the same group, both lines end up with both suppliers.

    Uses direct JSON append on the RFQItem.suppliers column (bypasses
    enrichment) since these are cross-applied candidates, not new discoveries.

    Args:
        rfq_number: The RFQ identifier.
        user_id: Current user's identifier (unused; accepted for interface consistency).

    Returns:
        {
            "added": int,    # total cross-applied supplier-line additions
            "details": [     # per-group breakdown
                {"group_label": str, "lines": [int], "suppliers_added": int}
            ]
        }
    """
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return {"error": f"RFQ '{rfq_number}' not found."}

        groups_data = rfq.item_groups
        if not groups_data:
            return {"added": 0, "details": []}

        groups = groups_data.get("groups", [])
        if not groups:
            return {"added": 0, "details": []}

        # Fetch all line items
        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()
        items_by_line = {it.line: it for it in items}

        total_added = 0
        details = []

        for g in groups:
            group_lines = g.get("lines", [])
            if len(group_lines) < 2:
                continue

            # Collect all unique suppliers across the group (keyed by name_lower)
            group_suppliers: dict[str, dict] = {}
            for gl in group_lines:
                item_obj = items_by_line.get(gl)
                if not item_obj:
                    continue
                for sup in (item_obj.suppliers or []):
                    if not isinstance(sup, dict):
                        continue
                    key = sup["name"].lower()
                    if key not in group_suppliers:
                        group_suppliers[key] = {
                            "supplier_id": sup.get("supplier_id"),
                            "name": sup["name"],
                            "contacts": sup.get("contacts", []),
                            "status": "candidate",
                            "price_type": "candidate",
                            "notes": (
                                "Cross-applied from group (supplier has history "
                                "with other items in this group)"
                            ),
                        }

            if not group_suppliers:
                continue

            # For each line in the group, add missing suppliers
            group_added = 0
            for gl in group_lines:
                item_obj = items_by_line.get(gl)
                if not item_obj:
                    continue
                current = list(item_obj.suppliers or [])
                existing_names = {s["name"].lower() for s in current if isinstance(s, dict)}

                to_add = [
                    sup for name_key, sup in group_suppliers.items()
                    if name_key not in existing_names
                ]
                if to_add:
                    current.extend(to_add)
                    item_obj.suppliers = current
                    flag_modified(item_obj, "suppliers")
                    group_added += len(to_add)

            session.commit()

            if group_added > 0:
                total_added += group_added
                details.append({
                    "group_label": g.get("label", "Unnamed"),
                    "lines": group_lines,
                    "suppliers_added": group_added,
                })

        return {"added": total_added, "details": details}
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()
