"""
RFQ CRUD helpers — synchronous DB operations called via asyncio.to_thread.

All public names are re-exported from quote_tools.py for backward compatibility.
"""

import datetime
import logging

from typing import Any

logger = logging.getLogger(__name__)

# Common aliases LLMs use for the "line" parameter
_LINE_ALIASES = ("line", "line_number", "item", "item_number")


def _get_line(data: dict):
    """Extract line number from data, accepting common aliases."""
    for key in _LINE_ALIASES:
        if key in data:
            return data[key]
    return None


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


def _rfq_to_dict(rfq) -> dict:
    """Convert an RFQ ORM object (with items loaded) to a plain dict
    compatible with the rendering functions."""
    return {
        "id": rfq.rfq_number,
        "customer": rfq.customer,
        "customer_id": str(rfq.customer_id) if rfq.customer_id else None,
        "customer_contact": rfq.customer_contact,
        "reference": rfq.reference,
        "netsuite_opportunity": rfq.netsuite_opportunity,
        "opportunity_id": str(rfq.opportunity_id) if rfq.opportunity_id else None,
        "hubspot_deal": rfq.hubspot_deal,
        "created_by": rfq.created_by,
        "created_date": str(rfq.created_date) if rfq.created_date else "",
        "assigned_to": rfq.assigned_to,
        "thread_id": rfq.thread_id,
        "status": rfq.status or "draft",
        "notes": rfq.notes or "",
        "history": rfq.history or [],
        "item_groups": rfq.item_groups,
        "pipeline_stage": getattr(rfq, "pipeline_stage", "unprocessed") or "unprocessed",
        "items": [_item_to_dict(item) for item in (rfq.items or [])],
    }


def _item_to_dict(item) -> dict:
    """Convert an RFQItem ORM object to a plain dict."""
    return {
        "line": item.line,
        "input_description": item.input_description or "",
        "input_code": item.input_code or "",
        "part_number": item.part_number,
        "brand": item.brand,
        "product_id": str(item.product_id) if item.product_id else None,
        "quantity": item.quantity,
        "uom": item.uom or "ea",
        "match": item.match or "unmatched",
        "notes": item.notes or "",
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
    except Exception:
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
            customer_contact=data.get("customer_contact"),
            reference=data.get("reference"),
            netsuite_opportunity=data.get("netsuite_opportunity"),
            hubspot_deal=data.get("hubspot_deal"),
            created_by=user_id,
            created_date=_today_date(),
            assigned_to=data.get("assigned_to", user_id),
            thread_id=data.get("thread_id"),
            status="draft",
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
    except Exception:
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    except Exception:
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


def _update_rfq_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Update RFQ header fields. Returns dict or error string."""
    from includes.dashboard.models import RFQ
    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        updatable = [
            "customer", "customer_id", "customer_contact", "reference", "notes",
            "netsuite_opportunity", "opportunity_id", "hubspot_deal", "assigned_to",
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
        if not changes:
            return f"Error: provide at least one of {', '.join(updatable)} to update."

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Updated RFQ: {', '.join(changes)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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

        updatable = [
            "input_description", "input_code", "part_number", "brand",
            "product_id", "quantity", "uom", "match", "notes",
        ]
        _no_clear = {"product_id", "match"}
        changes = []
        for key in updatable:
            if key in data:
                new_val = data[key]
                if key in _no_clear and not new_val and getattr(line_item, key, None):
                    continue
                setattr(line_item, key, new_val)
                changes.append(key)

        # If any identifying field changed and caller didn't explicitly set match,
        # reset classification (match → unmatched, clear product_id link)
        _identifying = {"input_description", "input_code", "part_number", "brand"}
        if _identifying & set(data.keys()) and "match" not in data:
            line_item.match = "unmatched"
            line_item.product_id = None
            changes.extend(["match", "product_id"])
            # Reset pipeline stage — item needs re-processing
            if rfq.pipeline_stage not in ("unprocessed",):
                rfq.pipeline_stage = "unprocessed"

        # Clear item groups — item descriptions/brands may have changed
        if changes:
            rfq.item_groups = None

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Updated line {line_num}: {', '.join(changes)}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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

        # Renumber remaining items using raw SQL to avoid ORM batch conflicts
        from sqlalchemy import text
        session.execute(
            text("UPDATE rfq_items SET line = line - 1 WHERE rfq_id = :rfq_id AND line > :line"),
            {"rfq_id": rfq.id, "line": line_num},
        )

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({"date": now, "user": user_id, "action": f"Deleted item {line_num}: {desc}"})
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _add_supplier_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Add supplier(s) to a line item. Returns full RFQ dict or error string."""
    from includes.dashboard.models import RFQ, RFQItem
    from includes.tools.quote_tools import _match_suppliers_to_db, _enrich_supplier_pricing

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

        suppliers_list = data.get("suppliers", [])
        if not suppliers_list and data.get("name"):
            suppliers_list = [data]
        if not suppliers_list:
            return "Error: 'name' or 'suppliers' list is required for add_supplier."

        def _has_contact_url(sup):
            """Check that at least one contact dict has a url field."""
            contacts = sup.get("contacts") or []
            if not isinstance(contacts, list):
                return False
            return any(
                isinstance(c, dict) and c.get("url")
                for c in contacts
            )

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
            return (
                f"REJECTED: All {len(skipped_names)} supplier(s) have blank or unknown names. "
                f"Please provide actual supplier names."
            )

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
            # Recombine: matched suppliers now have supplier_id set
            valid_suppliers = db_linked + needs_match
        else:
            valid_suppliers = db_linked

        # Auto-resolve product_id if the item has a part_number
        product_id = line_item.product_id
        if line_item.part_number:
            from includes.dashboard.models import Product as ProductModel
            
            # Validate existing product_id: does it actually match the part_number?
            if product_id:
                existing = session.query(ProductModel).filter(
                    ProductModel.id == product_id
                ).first()
                if not existing or (existing.part_number or "").strip().lower() != line_item.part_number.strip().lower():
                    logger.warning(
                        f"[auto-resolve] product_id={product_id} (part={existing.part_number if existing else 'N/A'}) "
                        f"does not match line part_number={line_item.part_number} — re-resolving"
                    )
                    product_id = None
                    line_item.product_id = None
            
            # Resolve from part_number if needed
            if not product_id:
                prod = session.query(ProductModel).filter(
                    ProductModel.part_number.ilike(line_item.part_number.strip())
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
                            "transaction_count"]:
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
                }
                current_suppliers.append(supplier_entry)
                existing_by_name[name.lower()] = supplier_entry
                added_names.append(name)

        # JSONB mutation — assign a new list and flag modified
        from sqlalchemy.orm.attributes import flag_modified
        line_item.suppliers = current_suppliers
        flag_modified(line_item, "suppliers")

        action_parts = []
        if added_names:
            action_parts.append(f"Added {len(added_names)} supplier(s) to line {line_num}: {', '.join(added_names)}")
        if updated_names:
            action_parts.append(f"Updated {len(updated_names)} existing supplier(s) on line {line_num}: {', '.join(updated_names)}")
        if skipped_names:
            action_parts.append(
                f"REJECTED {len(skipped_names)} supplier(s) — no contact URL provided: {', '.join(skipped_names)}. "
                f"Retry with contacts=[{{\"url\": \"https://...\"}}] for each."
            )

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": " | ".join(action_parts) or f"No changes to suppliers on line {line_num}",
        })
        # Auto-progress: draft → in_progress when suppliers are added
        if added_names and rfq.status == "draft":
            rfq.status = "in_progress"
            history.append({"date": now, "user": "system", "action": "Status auto-changed to in_progress (suppliers added)"})

        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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

        current_suppliers = list(line_item.suppliers or [])
        supplier = next((s for s in current_suppliers if s["name"] == name), None)
        if not supplier:
            return f"Error: supplier '{name}' not found on line {line_num}."

        # Normalize: agent sends 'currency', we store as 'cost_currency'
        if "currency" in data and "cost_currency" not in data:
            data["cost_currency"] = data["currency"]

        updatable = ["status", "price", "price_type", "cost_currency", "lead_time", "notes", "contacts", "purchase_ref"]
        changes = []
        for key in updatable:
            if key in data:
                supplier[key] = data[key]
                changes.append(key)

        from sqlalchemy.orm.attributes import flag_modified
        line_item.suppliers = current_suppliers
        flag_modified(line_item, "suppliers")

        now = _now_iso()
        history = list(rfq.history or [])
        history.append({
            "date": now, "user": user_id,
            "action": f"Updated supplier '{name}' on line {line_num}: {', '.join(changes)}",
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _update_status_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ
    new_status = data.get("status")
    valid = {"draft", "in_progress", "awaiting_quotes", "completed", "cancelled"}
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _link_external_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    from includes.dashboard.models import RFQ
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# =============================================================================
# Shared orchestration helpers — used by both tools (quote_tools.py) and
# action callbacks (rfq_actions.py).  All are synchronous, run via
# asyncio.to_thread.  No Chainlit dependencies.
# =============================================================================

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
            _update_item_sync(rfq_number, {"line": line, "match": match}, user_id)
            classified[match].append(line)
            if match == "specific":
                to_validate.append(item)
        else:
            unclassifiable.append(item)

    if search_db and to_validate:
        for item in to_validate:
            line = item["line"]
            part_number = item.get("part_number", "")
            brand = item.get("brand", "")
            try:
                product = _find_product_by_code(part_number, brand or None)
            except Exception:
                product = None
            if product:
                _update_item_sync(
                    rfq_number,
                    {"line": line, "part_number": product["part_number"],
                     "brand": product["brand"],
                     "product_id": product["id"], "match": "specific"},
                    user_id,
                )
                db_matches.append((
                    line, product["part_number"],
                    product["brand"], product["id"],
                ))

    return {
        "classified": classified,
        "db_matches": db_matches,
        "to_validate": to_validate,
        "unclassifiable": unclassifiable,
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
3. Is the brand correct?
4. If the part number is wrong, what is the correct one?

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
- "status": "confirmed" if everything matches, "discrepancy" if something is wrong, "not_found" if the part number doesn't exist online
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

        # Update RFQ items if discrepancies found
        session = _get_session()
        try:
            from includes.dashboard.models import RFQ, RFQItem
            rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
            if rfq:
                for v in validated:
                    if v.get("status") == "discrepancy":
                        item = (
                            session.query(RFQItem)
                            .filter(RFQItem.rfq_id == rfq.id, RFQItem.line == v["line"])
                            .first()
                        )
                        if item:
                            item.match = "discrepancy"
                            item.notes = v.get("findings", "")
                            if v.get("correct_part_number") and v["correct_part_number"] != item.part_number:
                                item.notes += f" (Correct PN: {v['correct_part_number']})"
            session.commit()
        except Exception as e:
            logger.warning(f"Failed to update discrepancy status: {e}")
            session.rollback()
        finally:
            session.close()

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
) -> list[dict]:
    """Search the web for suppliers of a product using Google Search grounding.

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
            f"\n\nAlready have these suppliers (do NOT include them): "
            f"{', '.join(existing_suppliers)}"
        )

    product_desc = description
    if part_number:
        product_desc = f"{part_number} - {description}"
    if brand:
        product_desc = f"{brand} {product_desc}"

    prompt = f"""Search the web for companies that sell or distribute this product:

Product: {product_desc}
{f"Part Number: {part_number}" if part_number else ""}
{f"Brand: {brand}" if brand else ""}
{f"Quantity needed: {quantity}" if quantity else ""}
{existing_str}

Find 3-5 suppliers. Prioritise authorised distributors and industrial wholesalers.
Search globally — do NOT restrict to Australia. For each supplier found, provide:
- name: Company name
- country: 2-letter ISO code (e.g. AU, US, GB, NZ)
- currency: Their trading currency (e.g. AUD, USD, NZD, GBP)
- website: Their website URL
- email: Contact email if visible (or empty string)
- phone: Contact phone if visible (or empty string)
- price: Numeric price if visible (or null)
- price_currency: Currency of the price (or null)
- notes: Brief description of what they supply and any relevant details

Return ONLY a JSON array of supplier objects. No other text.
Example: [{{"name": "Example Co", "country": "AU", "currency": "AUD", "website": "https://example.com", "email": "sales@example.com", "phone": "", "price": 24.99, "price_currency": "AUD", "notes": "Industrial distributor, stocks full range"}}]"""

    try:
        client = _genai.Client(http_options={"timeout": 120000})
        response = client.models.generate_content(
            model=Config.DEFAULT_MODEL,
            contents=prompt,
            config=_types.GenerateContentConfig(
                tools=[_types.Tool(google_search=_types.GoogleSearch())],
                temperature=0.2,
            ),
        )
        # Join ALL text parts — grounding splits text across multiple parts
        raw_text = ""
        try:
            if response.candidates:
                parts = response.candidates[0].content.parts or []
                raw_text = "".join(
                    p.text for p in parts
                    if hasattr(p, "text") and p.text
                ).strip()
        except (ValueError, AttributeError, IndexError):
            pass
        if not raw_text:
            try:
                raw_text = (response.text or "").strip()
            except (ValueError, AttributeError):
                pass
        # Clean markdown fences
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw_text = "\n".join(lines).strip()

        # Extract JSON array from any surrounding text
        json_start = raw_text.find("[")
        json_end = raw_text.rfind("]")
        if json_start != -1 and json_end != -1 and json_end > json_start:
            raw_text = raw_text[json_start:json_end + 1]

        suppliers_raw = json.loads(raw_text)
        if not isinstance(suppliers_raw, list):
            logger.warning(f"Web search returned non-list: {type(suppliers_raw)}")
            return []

        # Normalize to the format expected by _add_supplier_sync
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

        return results
    except json.JSONDecodeError as e:
        logger.error(f"Web search JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        return []
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return []


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
        except Exception:
            pass

        if suppliers:
            _add_supplier_sync(rfq_number, {"line": line, "suppliers": suppliers}, user_id)
            total_added += len(suppliers)
            by_line[line] = [s["name"] for s in suppliers]

    return {"added": total_added, "by_line": by_line}


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
            except Exception:
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return {"added": total_added, "by_line": by_line}


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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
