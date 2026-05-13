"""
RFQ (Request for Quote) management tools for LangGraph agents.

Provides tools to create, update, and query RFQs stored in the
``rfqs`` and ``rfq_items`` SQL tables (PostgreSQL).
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import chainlit as cl
from langchain_core.tools import tool

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
# SQL helpers — all DB access runs in a thread via asyncio.to_thread
# ---------------------------------------------------------------------------

def _get_session():
    from includes.dashboard.database import get_session
    return get_session()


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
    }


def _get_rfq_sync(rfq_number: str):
    """Fetch a single RFQ by rfq_number, with items eagerly loaded. Returns (rfq_orm, session)."""
    from includes.dashboard.models import RFQ
    session = _get_session()
    rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
    return rfq, session


async def _next_rfq_number(store) -> str:
    """Async wrapper kept for backward compat during migration."""
    return await asyncio.to_thread(_next_rfq_number_sync)


def _verify_supplier_url(
    name: str,
    url: str | None,
    country: str | None = None,
    product_hint: str = "",
) -> str | None:
    """Verify a supplier URL is reachable; if not, search for the correct one.

    1. HTTP HEAD the URL (follows redirects).  If it returns 200 with content,
       return the original URL.
    2. If the request fails or returns an empty/error response, use Gemini with
       Google Search grounding to find the correct website for the supplier.
    3. Return the corrected URL, or the original if nothing better is found.
    """
    import urllib.request

    if not url:
        return _search_supplier_url(name, country, product_hint=product_hint)

    # Normalise: ensure scheme is present
    check_url = url if "://" in url else f"https://{url}"

    # HTTP HEAD check (timeout 5s)
    try:
        req = urllib.request.Request(
            check_url, method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (compatible; EagleAgent/1.0)"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        if resp.status == 200:
            logger.debug(f"[url-verify] HTTP OK for {check_url}")
            return url
        else:
            logger.info(f"[url-verify] HTTP {resp.status} for {check_url}, searching for correct URL")
    except Exception as e:
        logger.info(f"[url-verify] HTTP failed for {check_url} ({e}), searching for correct URL")

    return _search_supplier_url(name, country, product_hint=product_hint) or url


def _search_supplier_url(
    name: str,
    country: str | None = None,
    product_hint: str = "",
) -> str | None:
    """Use Gemini with Google Search grounding to find a supplier's real website URL."""
    import urllib.request

    try:
        from google import genai as _genai
        from google.genai import types as _types

        location = f" in {country}" if country else ""
        product_ctx = f" They supply {product_hint}." if product_hint else ""
        prompt = (
            f"What is the official website URL for the industrial/commercial supplier "
            f"'{name}'{location}?{product_ctx} "
            f"Return ONLY the URL (e.g. https://example.com), nothing else. "
            f"If you cannot find it, return NONE."
        )

        client = _genai.Client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_types.GenerateContentConfig(
                tools=[_types.Tool(google_search=_types.GoogleSearch())],
                temperature=0.0,
            ),
        )
        result = (response.text or "").strip()
        if result and result.upper() != "NONE" and "." in result:
            # Clean up: extract just the URL if extra text crept in
            for token in result.split():
                if "." in token and ("/" in token or token.startswith("http")):
                    url = token.strip("`\"'<>")
                    if not url.startswith("http"):
                        url = f"https://{url}"
                    # Verify the found URL is actually reachable (HTTP HEAD)
                    try:
                        req = urllib.request.Request(
                            url, method="HEAD",
                            headers={"User-Agent": "Mozilla/5.0 (compatible; EagleAgent/1.0)"},
                        )
                        resp = urllib.request.urlopen(req, timeout=5)
                        if resp.status == 200:
                            logger.info(f"[url-search] Found URL for '{name}': {url}")
                            return url
                        else:
                            logger.warning(f"[url-search] Found URL {url} for '{name}' but got HTTP {resp.status}")
                            continue
                    except Exception:
                        logger.warning(f"[url-search] Found URL {url} for '{name}' but HTTP check failed")
                        continue
        logger.info(f"[url-search] No valid URL found for '{name}'")
        return None
    except Exception as e:
        logger.warning(f"[url-search] Search failed for '{name}': {e}")
        return None


def _match_suppliers_to_db(suppliers: list[dict], product_hint: str = "") -> None:
    """Fuzzy-match supplier names against the DB and enrich with supplier_id + contacts.

    Uses the stricter match_supplier() which verifies domain and country
    in addition to name similarity.
    Mutates supplier dicts in-place.

    Args:
        suppliers: List of supplier dicts to match/create.
        product_hint: Optional product context (part number, brand, description)
                      used to improve URL search accuracy when verification fails.
    """
    # Collect names that need matching (no supplier_id yet)
    names_to_match = {}  # lower name -> list of supplier dicts
    for sup in suppliers:
        if sup.get("supplier_id"):
            continue
        name = (sup.get("name") or "").strip()
        if name:
            names_to_match.setdefault(name.lower(), []).append(sup)

    if not names_to_match:
        return

    try:
        from includes.dashboard.database import (
            get_session,
            match_supplier,
            merge_supplier_contacts,
        )
        from includes.dashboard.models import Supplier
    except ImportError:
        logger.warning("Cannot import DB models for supplier matching")
        return

    session = get_session()
    try:
        for name_lower, sup_list in names_to_match.items():
            # Extract url and country from the supplier dict for verification
            sup_url = None
            for c in sup_list[0].get("contacts", []):
                if isinstance(c, dict) and c.get("url"):
                    sup_url = c["url"]
                    break
            sup_country = sup_list[0].get("country")

            # Verify/correct the URL before matching or persisting
            verified_url = _verify_supplier_url(
                sup_list[0].get("name", "").strip(),
                sup_url,
                sup_country,
                product_hint=product_hint,
            )
            if verified_url != sup_url:
                if verified_url:
                    logger.info(
                        f"[url-verify] Corrected URL for '{sup_list[0].get('name')}': "
                        f"{sup_url} → {verified_url}"
                    )
                else:
                    logger.info(
                        f"[url-verify] Could not verify URL for '{sup_list[0].get('name')}': {sup_url}"
                    )
                # Update the contacts in the supplier dicts
                for sup in sup_list:
                    for c in sup.get("contacts", []):
                        if isinstance(c, dict) and c.get("url") == sup_url:
                            c["url"] = verified_url or sup_url
                sup_url = verified_url or sup_url

            row = match_supplier(name_lower, url=sup_url, country=sup_country, session=session)
            if row:
                logger.info(
                    f"[supplier-match] '{name_lower}' → '{row.name}' (id={row.id})"
                )
                for sup in sup_list:
                    sup["supplier_id"] = str(row.id)
                    if row.contacts:
                        merge_supplier_contacts(sup, row.contacts)
            else:
                # No match — create a new web-sourced Supplier record
                ref_sup = sup_list[0]
                new_supplier = Supplier(
                    name=ref_sup.get("name", "").strip(),
                    country=ref_sup.get("country"),
                    currency=ref_sup.get("currency"),
                    url=sup_url,
                    contacts=ref_sup.get("contacts"),
                    source="web",
                )
                session.add(new_supplier)
                session.flush()  # get the generated id
                logger.info(
                    f"[supplier-create] Created new web supplier '{new_supplier.name}' (id={new_supplier.id})"
                )

                # Run proper categorization using the full taxonomy
                try:
                    from includes.supplier_categorization import (
                        categorize_supplier,
                        load_taxonomy,
                    )
                    from google import genai as _genai

                    _client = _genai.Client()
                    _taxonomy = load_taxonomy()
                    _cat_input = {
                        "name": new_supplier.name,
                        "url": new_supplier.url,
                        "city": None,
                        "country": new_supplier.country,
                        "purchase_count": 0,
                    }
                    cat_result = categorize_supplier(
                        _client, "gemini-2.5-flash", _taxonomy, _cat_input
                    )
                    new_supplier.supply_chain_position = {
                        "category": cat_result.get("category"),
                        "tier": cat_result.get("tier"),
                        "confidence": cat_result.get("confidence"),
                        "reasoning": cat_result.get("reasoning"),
                    }
                    new_supplier.modified_by = "ai:categorizer"
                    session.flush()
                    logger.info(
                        f"[supplier-categorize] '{new_supplier.name}' → "
                        f"{cat_result.get('tier')}/{cat_result.get('category')} "
                        f"(confidence={cat_result.get('confidence')})"
                    )
                    # Update supplier dicts with proper categorization
                    for sup in sup_list:
                        if cat_result.get("tier"):
                            sup["tier"] = cat_result["tier"]
                        if cat_result.get("category"):
                            sup["category"] = cat_result["category"]
                except Exception as cat_err:
                    logger.warning(
                        f"[supplier-categorize] Failed for '{new_supplier.name}': {cat_err}"
                    )
                    # Fall back to whatever the agent provided
                    if ref_sup.get("tier") and ref_sup.get("category"):
                        new_supplier.supply_chain_position = {
                            "tier": ref_sup["tier"],
                            "category": ref_sup["category"],
                        }

                for sup in sup_list:
                    sup["supplier_id"] = str(new_supplier.id)
        session.commit()
    except Exception as e:
        logger.warning(f"Supplier DB matching failed: {e}")
    finally:
        session.close()


def _enrich_supplier_pricing(suppliers: list[dict], product_id: str | None) -> None:
    """Look up cost/sale pricing for each supplier+product and enrich in-place.

    Finds the most recent SalesOrder or Quote transaction for the pair and
    reads both ``cost`` (buy price) and ``price`` (sell price) from it.

    Adds to each supplier dict:
      - cost_price, sale_price, price_date, price_doc, price_doc_type
      - transaction_count  (total SO + Quote transactions for this pair)
    """
    if not product_id:
        return

    from sqlalchemy import and_, desc, func
    import uuid

    try:
        pid = uuid.UUID(str(product_id))
    except (ValueError, TypeError):
        return

    sids = {}  # supplier_id str -> supplier dict
    for sup in suppliers:
        sid = sup.get("supplier_id")
        if sid:
            sids[str(sid)] = sup

    if not sids:
        return

    from includes.dashboard.models import Transaction, Supplier
    session = _get_session()
    try:
        for sid_str, sup in sids.items():
            try:
                sid = uuid.UUID(sid_str)
            except (ValueError, TypeError):
                continue

            # Look up supplier currency
            sup_currency = (
                session.query(Supplier.currency)
                .filter(Supplier.id == sid)
                .scalar()
            )

            base_filter = and_(
                Transaction.supplier_id == sid,
                Transaction.product_id == pid,
                Transaction.doc_type.in_(["SalesOrder", "Quote"]),
            )

            # Most recent SO or Quote — single source for cost + sale
            latest = (
                session.query(
                    Transaction.cost, Transaction.price,
                    Transaction.date, Transaction.doc_number, Transaction.doc_type,
                )
                .filter(base_filter)
                .order_by(desc(Transaction.date))
                .first()
            )
            if latest:
                if latest.cost is not None:
                    sup["cost_price"] = float(latest.cost)
                if latest.price is not None:
                    sup["sale_price"] = float(latest.price)
                sup["price_date"] = latest.date.isoformat() if latest.date else None
                sup["price_doc"] = latest.doc_number
                sup["price_doc_type"] = latest.doc_type
                if sup_currency and sup_currency != "AUD":
                    sup["cost_currency"] = sup_currency
                    # Convert cost to AUD for margin calculation
                    if latest.cost is not None:
                        try:
                            from includes.currency import convert_to_aud
                            sup["cost_price_aud"] = round(convert_to_aud(float(latest.cost), sup_currency), 2)
                        except Exception as exc:
                            logger.warning(f"Currency conversion {sup_currency}→AUD failed: {exc}")

            # Count of SO + Quote transactions
            txn_count = (
                session.query(func.count(Transaction.id))
                .filter(base_filter)
                .scalar()
            )
            if txn_count:
                sup["transaction_count"] = txn_count

    except Exception as e:
        logger.warning(f"Pricing enrichment failed: {e}")
    finally:
        session.close()


def _render_rfq_summary(rfq: dict) -> str:
    """Render a single RFQ as a markdown summary block."""
    status_display = rfq.get("status", "draft").replace("_", " ").title()
    assigned = rfq.get("assigned_to", "Unassigned")
    customer = rfq.get("customer", "Unknown")
    created = rfq.get("created_date", "")
    rfq_id = rfq.get("id", "???")
    notes = rfq.get("notes", "")
    ref = rfq.get("reference", "")
    netsuite = rfq.get("netsuite_opportunity", "")
    hubspot = rfq.get("hubspot_deal", "")

    items = rfq.get("items", [])
    total = len(items)
    confirmed = sum(1 for i in items if i.get("status") == "confirmed")
    identified = sum(1 for i in items if i.get("status") == "identified")
    review = sum(1 for i in items if i.get("status") == "review")
    unidentified = sum(1 for i in items if i.get("status") == "unidentified")
    with_suppliers = sum(1 for i in items if i.get("suppliers"))

    lines = [f"## 📋 {rfq_id} — {customer}"]
    meta = [f"**Status:** {status_display}", f"**Assigned to:** {assigned}"]
    if created:
        meta.append(f"**Created:** {created}")
    lines.append(" | ".join(meta))

    if ref:
        lines.append(f"**Reference:** {ref}")
    ext_links = []
    if netsuite:
        ext_links.append(f"NetSuite: {netsuite}")
    if hubspot:
        ext_links.append(f"HubSpot: {hubspot}")
    if ext_links:
        lines.append(" | ".join(ext_links))

    contact = rfq.get("customer_contact")
    if contact:
        parts = []
        if contact.get("name"):
            parts.append(contact["name"])
        if contact.get("email"):
            parts.append(contact["email"])
        if contact.get("phone"):
            parts.append(contact["phone"])
        if parts:
            lines.append(f"**Contact:** {' · '.join(parts)}")

    if notes:
        lines.append(f"**Notes:** {notes}")

    lines.append("")

    if items:
        status_icons = {
            "confirmed": "✅ Confirmed",
            "identified": "🔵 Identified",
            "review": "🟡 Needs Review",
            "unidentified": "⚠️ Unidentified",
        }
        lines.append("| # | Description | Part Number | Brand | Qty | Status | Suppliers |")
        lines.append("|---|------------|-------------|-------|-----|--------|-----------|")
        for item in items:
            line_num = item.get("line", "")
            desc = item.get("input_description", "")
            pn = item.get("part_number") or item.get("input_code") or "—"
            brand = item.get("brand") or "—"
            qty = item.get("quantity", "")
            uom = item.get("uom", "")
            qty_str = f"{qty} {uom}".strip() if qty else "—"
            status = status_icons.get(item.get("status", ""), item.get("status", ""))
            item_notes = item.get("notes", "")
            if item_notes:
                status += f" ({item_notes})"
            suppliers = item.get("suppliers", [])
            if suppliers:
                sup_parts = []
                _status_labels = {
                    "estimated": "est",
                    "previous_purchase": "prev",
                    "previous_quote": "prev",
                    "quoted": "quoted",
                }
                for s in suppliers:
                    name = s.get("name", "?")
                    price = s.get("price")
                    st = s.get("status", "candidate")
                    price_type = s.get("price_type", "")
                    cost_price = s.get("cost_price")
                    sale_price = s.get("sale_price")
                    if st == "dropped":
                        sup_parts.append(f"~~{name}~~")
                    else:
                        parts = [name]
                        # Show cost/sale if available
                        if cost_price is not None or sale_price is not None:
                            price_bits = []
                            if cost_price is not None:
                                try:
                                    price_bits.append(f"cost ${float(cost_price):,.2f}")
                                except (ValueError, TypeError):
                                    pass
                            if sale_price is not None:
                                try:
                                    price_bits.append(f"sale ${float(sale_price):,.2f}")
                                except (ValueError, TypeError):
                                    pass
                            if cost_price and sale_price:
                                try:
                                    cost_for_margin = float(s.get("cost_price_aud") or cost_price)
                                    margin = (float(sale_price) - cost_for_margin) / float(sale_price) * 100
                                    price_bits.append(f"{margin:.0f}%")
                                except (ValueError, TypeError, ZeroDivisionError):
                                    pass
                            if price_bits:
                                parts.append(" / ".join(price_bits))
                        elif price is not None:
                            label = _status_labels.get(price_type, "")
                            try:
                                price_str = f"${float(price):,.2f}"
                            except (ValueError, TypeError):
                                price_str = str(price)
                            if label:
                                parts.append(f"{price_str} {label}")
                            else:
                                parts.append(price_str)
                        sup_parts.append(" — ".join(parts) if len(parts) > 1 else parts[0])
                sup_str = "<br>".join(sup_parts)
            else:
                sup_str = "—"
            lines.append(
                f"| {line_num} | {desc} | {pn} | {brand} | {qty_str} | {status} | {sup_str} |"
            )

        lines.append("")
        counts = []
        if confirmed:
            counts.append(f"{confirmed} confirmed")
        if identified:
            counts.append(f"{identified} identified")
        if review:
            counts.append(f"{review} needs review")
        if unidentified:
            counts.append(f"{unidentified} unidentified")
        lines.append(
            f"**{total} items** | {', '.join(counts) if counts else 'none'} | "
            f"{with_suppliers} with suppliers"
        )
    else:
        lines.append("*No items yet.*")

    return "\n".join(lines)


async def _notify_rfq_updated() -> None:
    """Notify the dashboard to refresh after RFQ data changes."""
    from includes.agent_bridge import notify_dashboard
    await notify_dashboard("dashboard_refresh")


def _render_rfq_list(rfqs: list[dict]) -> str:
    """Render a summary table of multiple RFQs."""
    if not rfqs:
        return "No RFQs found."

    lines = ["## 📋 RFQ List", ""]
    lines.append("| RFQ | Customer | Status | Items | Assigned | Created |")
    lines.append("|-----|----------|--------|-------|----------|---------|")
    for rfq in rfqs:
        rfq_id = rfq.get("id", "???")
        customer = rfq.get("customer", "—")
        status = rfq.get("status", "draft").replace("_", " ").title()
        items = rfq.get("items", [])
        total = len(items)
        confirmed = sum(1 for i in items if i.get("status") == "confirmed")
        assigned = rfq.get("assigned_to", "—")
        created = rfq.get("created_date", "")
        lines.append(
            f"| [{rfq_id}](/rfqs/{rfq_id}) | {customer} | {status} | {confirmed}/{total} confirmed | {assigned} | {created} |"
        )
    lines.append("")
    lines.append(f"**{len(rfqs)} RFQs total**")
    return "\n".join(lines)



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
            name = (sup.get("name") or "").strip()
            has_db_link = bool(sup.get("supplier_id"))
            if not has_db_link and name.lower() in _bad_names:
                skipped_names.append(name or "Unknown")
            elif not has_db_link and not _has_contact_url(sup):
                skipped_names.append(name or "Unknown")
            else:
                valid_suppliers.append(sup)

        if skipped_names and not valid_suppliers:
            return (
                f"REJECTED: All {len(skipped_names)} supplier(s) were rejected because they have no contact URL: "
                f"{', '.join(skipped_names)}. "
                f"You MUST provide contacts with at least a url (website) for each supplier. "
                f"Look up their website and retry with: contacts=[{{\"url\": \"https://...\"}}]"
            )
        if skipped_names and valid_suppliers:
            # Partial rejection — continue with valid ones but warn loudly
            pass

        _match_suppliers_to_db(
            valid_suppliers,
            product_hint=" ".join(filter(None, [
                line_item.part_number,
                line_item.brand,
                line_item.input_description,
            ])),
        )

        # Auto-resolve product_id if the item has a part_number but no product_id
        product_id = line_item.product_id
        if not product_id and line_item.part_number:
            from includes.dashboard.models import Product as ProductModel
            prod = session.query(ProductModel).filter(
                ProductModel.part_number.ilike(line_item.part_number)
            ).first()
            if prod:
                line_item.product_id = prod.id
                product_id = prod.id
                logger.info(f"[auto-resolve] Set product_id={prod.id} for part_number={line_item.part_number}")

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

        updatable = ["status", "price", "price_type", "lead_time", "notes", "contacts", "purchase_ref"]
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


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------

def create_quote_tools(user_id: str) -> list:
    """Create RFQ management tools bound to a user.

    Args:
        user_id: The current user's identifier (email)

    Returns:
        List of tools [manage_rfq, get_rfq]
    """

    @tool
    async def manage_rfq(
        action: str,
        rfq_id: Optional[str] = None,
        data: Optional[Any] = None,
    ) -> str:
        """Create or update an RFQ (Request for Quote).

        Actions:
          create        — Create a new RFQ. data keys: customer (required),
                          customer_contact ({name, email, phone}), reference,
                          netsuite_opportunity, hubspot_deal, notes,
                          items ([{input_description, input_code, part_number,
                          brand, quantity, uom}])
          update        — Update top-level RFQ properties. data keys: any of
                          customer, customer_contact, reference, notes,
                          netsuite_opportunity, hubspot_deal, assigned_to
          update_item   — Update an RFQ line item. data keys: line (required, int),
                          plus any of: input_description, input_code, part_number,
                          brand, product_id, quantity, uom, status, notes.
                          Item status values: unidentified, identified, confirmed,
                          review (needs human attention — e.g. part number
                          discrepancy found during web search)
          add_items     — Add multiple line items to an existing RFQ. data keys:
                          items (required, list of dicts with input_description,
                          input_code, part_number, brand, quantity, uom).
                          Use this instead of create when the RFQ already exists.
          add_supplier  — Add supplier candidate(s) to a line item. data keys:
                          line (required), EITHER name (required) for a single
                          supplier with optional supplier_id, contacts, status,
                          price, price_type, lead_time, notes, purchase_ref;
                          OR suppliers (list of dicts with those same keys)
                          to add multiple at once.
                          contacts: list of dicts, each with any of: url
                          (website), email, phone, city, state, country.
                          MANDATORY: you MUST include contacts with at least
                          a url for every supplier. A supplier without
                          contact details is useless — do not add one.
                          Supplier status values: candidate (default),
                          shortlisted, selected, dropped.
                          Price type values: estimated (price from web search),
                          previous_purchase (price from purchase history),
                          previous_quote (price from past quote), quoted
                          (new quote received). Omit if no price.
                          purchase_ref: optional dict {doc_number, date,
                          order_count} linking to the latest purchase record.
          update_supplier — Update a supplier on a line item. data keys:
                          line (required), name (required), plus any of: status,
                          price, price_type, lead_time, notes, contacts,
                          purchase_ref
          clear_suppliers — Remove all suppliers from line item(s). data keys:
                          line (optional, int — if omitted clears ALL lines)
          assign        — Reassign the RFQ. data keys: assigned_to (required)
          update_status — Change RFQ status. data keys: status (required, one of
                          draft/in_progress/awaiting_quotes/completed/cancelled)
          add_note      — Append a note. data keys: note (required)
          link_external — Set external IDs. data keys: netsuite_opportunity
                          and/or hubspot_deal

        Args:
            action: The mutation to perform (see above).
            rfq_id: The RFQ identifier (required for all actions except create).
            data: Action-specific payload (see above).
        """
        data = data or {}

        # Gemini models sometimes pass data as a JSON string instead of a dict.
        if isinstance(data, str):
            import json
            logger.warning(f"manage_rfq: 'data' received as string, parsing JSON: {data[:200]}")
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                return f"Error: 'data' must be a JSON object, got unparseable string: {data[:100]}"

        # For create: inject Chainlit's thread_id from the current session
        if action == "create":
            try:
                import chainlit as cl
                thread_id = cl.context.session.thread_id
                if thread_id:
                    data["thread_id"] = thread_id
            except Exception:
                pass

        _ACTION_MAP = {
            "create": lambda: asyncio.to_thread(_create_rfq_sync, data, user_id),
            "update": lambda: asyncio.to_thread(_update_rfq_sync, rfq_id, data, user_id),
            "update_item": lambda: asyncio.to_thread(_update_item_sync, rfq_id, data, user_id),
            "add_items": lambda: asyncio.to_thread(_add_items_sync, rfq_id, data, user_id),
            "add_supplier": lambda: asyncio.to_thread(_add_supplier_sync, rfq_id, data, user_id),
            "update_supplier": lambda: asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_id),
            "clear_suppliers": lambda: asyncio.to_thread(_clear_suppliers_sync, rfq_id, data, user_id),
            "assign": lambda: asyncio.to_thread(_assign_sync, rfq_id, data, user_id),
            "update_status": lambda: asyncio.to_thread(_update_status_sync, rfq_id, data, user_id),
            "add_note": lambda: asyncio.to_thread(_add_note_sync, rfq_id, data, user_id),
            "link_external": lambda: asyncio.to_thread(_link_external_sync, rfq_id, data, user_id),
        }

        handler = _ACTION_MAP.get(action)
        if not handler:
            return (
                f"Error: unknown action '{action}'. Valid actions: create, "
                "update, update_item, add_items, add_supplier, update_supplier, "
                "clear_suppliers, assign, update_status, add_note, link_external."
            )

        if action != "create" and not rfq_id:
            return "Error: rfq_id is required for this action."

        result = await handler()

        # Error string returned from sync helper
        if isinstance(result, str):
            return result

        # Error dict from create
        if isinstance(result, dict) and "error" in result:
            return result["error"]

        # Name the thread after RFQ creation
        if action == "create" and isinstance(result, dict) and data.get("thread_id"):
            try:
                import chainlit as cl
                data_layer = cl.data._data_layer
                if data_layer:
                    thread_name = f"{result.get('id', '')} — {result.get('customer', '')}"
                    await data_layer.update_thread(
                        thread_id=data["thread_id"],
                        name=thread_name,
                    )
            except Exception as e:
                logger.warning(f"Failed to name thread: {e}")

        await _notify_rfq_updated()
        return _render_rfq_summary(result)

    @tool
    async def get_rfq(
        rfq_id: Optional[str] = None,
        list_all: bool = False,
        assigned_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """Retrieve RFQ details or list RFQs.

        Usage:
          get_rfq(rfq_id="RFQ-2026-0042")       — full detail of one RFQ
          get_rfq(list_all=True)                  — summary list of all RFQs
          get_rfq(assigned_to="tom@eagle.com.au") — RFQs assigned to a user
          get_rfq(status="in_progress")           — filter by status

        Args:
            rfq_id: Specific RFQ identifier to retrieve.
            list_all: If True, return a summary of all RFQs.
            assigned_to: Filter RFQs by assignee email.
            status: Filter RFQs by status.
        """
        if rfq_id:
            rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
            if not rfq:
                return f"RFQ '{rfq_id}' not found."
            await _notify_rfq_updated()
            return _render_rfq_summary(rfq)

        rfqs = await asyncio.to_thread(_list_rfqs_sync,
                                        assigned_to if assigned_to else None,
                                        status if status else None)

        if not list_all and not assigned_to and not status:
            rfqs = [r for r in rfqs if r.get("assigned_to") == user_id]
            if not rfqs:
                return "You have no RFQs assigned. Use `get_rfq(list_all=True)` to see all."

        return _render_rfq_list(rfqs)

    return [manage_rfq, get_rfq]
