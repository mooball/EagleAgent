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
        "customer_contact": rfq.customer_contact,
        "reference": rfq.reference,
        "netsuite_opportunity": rfq.netsuite_opportunity,
        "hubspot_deal": rfq.hubspot_deal,
        "created_by": rfq.created_by,
        "created_date": str(rfq.created_date) if rfq.created_date else "",
        "assigned_to": rfq.assigned_to,
        "thread_id": rfq.thread_id,
        "status": rfq.status or "draft",
        "notes": rfq.notes or "",
        "history": rfq.history or [],
        "item_groups": rfq.item_groups,
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
        "status": item.status or "unidentified",
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
                status=raw.get("status", "unidentified"),
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
                status=raw.get("status", "unidentified"),
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
            "customer", "customer_contact", "reference", "notes",
            "netsuite_opportunity", "hubspot_deal", "assigned_to",
        ]
        changes = []
        for key in updatable:
            if key in data:
                setattr(rfq, key, data[key])
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
            "product_id", "quantity", "uom", "status", "notes",
        ]
        _no_clear = {"part_number", "brand", "product_id", "input_description", "input_code"}
        changes = []
        for key in updatable:
            if key in data:
                new_val = data[key]
                if key in _no_clear and not new_val and getattr(line_item, key, None):
                    continue
                setattr(line_item, key, new_val)
                changes.append(key)

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
                if new_status != "candidate" or existing.get("status") == "candidate":
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
