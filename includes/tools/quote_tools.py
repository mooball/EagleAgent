"""
RFQ (Request for Quote) management tools for LangGraph agents.

Provides tools to create, update, and query RFQs stored in the
``rfqs`` and ``rfq_items`` SQL tables (PostgreSQL).
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import chainlit as cl
from langchain_core.tools import tool

# Re-export everything from sub-modules so existing imports keep working
from includes.tools.rfq_crud import (  # noqa: F401
    _get_line, _now_iso, _today, _today_date, _now_dt,
    _get_session, _next_rfq_number_sync, _rfq_to_dict, _item_to_dict,
    _get_rfq_sync, _create_rfq_sync, _get_rfq_dict_sync, _add_items_sync,
    _list_rfqs_sync, _update_rfq_sync, _update_item_sync, _delete_item_sync,
    _add_supplier_sync, _update_supplier_sync, _clear_suppliers_sync,
    _assign_sync, _update_status_sync, _add_note_sync, _link_external_sync,
    _update_item_groups_sync,
    _find_brand_suppliers_sync, _cross_apply_suppliers_sync,
    _select_quote_sync, _decline_quote_sync, _set_supplier_meta_sync,
)
from includes.tools.rfq_render import (  # noqa: F401
    _render_rfq_summary, _render_rfq_list, _render_rfq_brief_summary,
)
from config.settings import Config

logger = logging.getLogger(__name__)


def _build_quotation_snapshot(rfq: dict) -> str:
    """Build a comprehensive Markdown snapshot of an RFQ's quotation state.

    Dual-view structure:
      A. Header + item×supplier price matrix (compact comparison)
      B. Per-supplier detail sections (shipping, notes, terms, item list)
      C. Totals row

    Args:
        rfq: RFQ dict from _get_rfq_dict_sync / _rfq_to_dict.

    Returns:
        Markdown string suitable for LLM consumption.
    """
    lines = []

    # ── Header ──────────────────────────────────────────────────────────
    rfq_id = rfq.get("id", "???")
    customer = rfq.get("customer", "Unknown")
    status = (rfq.get("status") or "draft").replace("_", " ").title()
    assigned = rfq.get("assigned_to", "Unassigned")
    created = rfq.get("created_date", "")
    ref = rfq.get("reference", "")
    ns_opp = rfq.get("netsuite_opportunity", "")
    hubspot = rfq.get("hubspot_deal", "")
    customer_id = rfq.get("customer_id", "")
    contact = rfq.get("customer_contact") or {}

    lines.append(f"# {rfq_id} — Quotation Status")
    header_meta = [f"**Customer:** {customer}"]
    if customer_id:
        header_meta[-1] += f" (ID: {customer_id})"
    if contact:
        parts = []
        if contact.get("name"):
            parts.append(contact["name"])
        if contact.get("email"):
            parts.append(contact["email"])
        if contact.get("phone"):
            parts.append(contact["phone"])
        if parts:
            header_meta[-1] += f" | **Contact:** {' — '.join(parts)}"
    header_meta.append(f"**Status:** {status}")
    if created:
        header_meta.append(f"**Created:** {created}")
    header_meta.append(f"**Assigned to:** {assigned}")
    lines.append(" | ".join(header_meta))

    ext_ids = []
    if ref:
        ext_ids.append(f"**Reference:** {ref}")
    if ns_opp:
        ext_ids.append(f"**NetSuite Opp:** {ns_opp}")
    if hubspot:
        ext_ids.append(f"**HubSpot:** {hubspot}")
    if ext_ids:
        lines.append(" | ".join(ext_ids))

    items = rfq.get("items", [])
    supplier_meta = rfq.get("supplier_meta") or {}

    # ── Collect unique shortlisted suppliers ─────────────────────────────
    supplier_names = []
    for item in items:
        for s in (item.get("suppliers") or []):
            name = s.get("name", "")
            if name and s.get("status") in ("shortlisted", "selected") and name not in supplier_names:
                supplier_names.append(name)

    if not items:
        lines.append("\n*No items on this RFQ.*")
        return "\n".join(lines)

    # ── Section A: Price Matrix ──────────────────────────────────────────
    lines.append("\n## Price Matrix")
    # Build header: # | Description | Part # | NS ID | Brand | Qty | Cost | Sale | Supplier...
    matrix_header = ["#", "Description", "Part #", "NS ID", "Brand", "Qty", "Cost", "Sale"]
    matrix_header.extend(supplier_names)
    lines.append("| " + " | ".join(matrix_header) + " |")
    lines.append("|" + "|".join(["---"] * len(matrix_header)) + "|")

    total_cost = 0
    total_sale = 0

    for item in items:
        line_no = item.get("line", "?")
        desc = (item.get("input_description") or "—")[:60]
        pn = item.get("part_number") or "—"
        ns_id = item.get("input_code") or "—"  # NetSuite internal ID
        brand = item.get("brand") or "—"
        qty = item.get("quantity") or 0
        cost = item.get("cost_price")
        sale = item.get("sale_price")

        if cost:
            total_cost += float(cost) * (qty or 0)
        if sale:
            total_sale += float(sale) * (qty or 0)

        cost_str = f"${float(cost):,.2f}" if cost else "—"
        sale_str = f"${float(sale):,.2f}" if sale else "—"

        row = [
            str(line_no), desc, pn, ns_id, brand, str(qty), cost_str, sale_str
        ]

        # Supplier columns
        for sup_name in supplier_names:
            cell = "—"
            for s in (item.get("suppliers") or []):
                if s.get("name") == sup_name and s.get("status") in ("shortlisted", "selected"):
                    qc = s.get("quote_cost")
                    qs = s.get("quote_status")
                    curr = s.get("quote_currency")
                    if qs == "declined":
                        cell = "✗"
                    elif qc is not None:
                        prefix = "$" if (not curr or curr == "AUD") else f"{curr} "
                        if qs == "selected":
                            cell = f"{prefix}{float(qc):,.2f} ★"
                        else:
                            cell = f"{prefix}{float(qc):,.2f}"
                    break
            row.append(cell)

        lines.append("| " + " | ".join(row) + " |")

    lines.append(f"\n**Key:** ★ = selected, ✗ = declined, — = not quoted")
    lines.append(f"**Totals:** Cost ${total_cost:,.2f} | Sale ${total_sale:,.2f}")

    # ── Section B: Per-Supplier Detail ───────────────────────────────────
    if supplier_names:
        lines.append("\n## Supplier Details")
        for sup_name in supplier_names:
            meta = supplier_meta.get(sup_name, {})
            shipping = meta.get("shipping_cost")
            shipping_curr = meta.get("shipping_currency") or "AUD"
            notes = meta.get("notes") or ""
            terms = meta.get("terms") or ""

            lines.append(f"\n### {sup_name}")
            meta_parts = []
            if shipping is not None:
                prefix = "$" if shipping_curr == "AUD" else f"{shipping_curr} "
                meta_parts.append(f"**Shipping:** {prefix}{float(shipping):,.2f}")
            else:
                meta_parts.append("**Shipping:** —")
            if notes:
                meta_parts.append(f"**Notes:** {notes}")
            if terms:
                meta_parts.append(f"**Terms:** {terms}")
            lines.append(" | ".join(meta_parts))

            # Item sub-table for this supplier
            sup_items = []
            for item in items:
                for s in (item.get("suppliers") or []):
                    if s.get("name") == sup_name and s.get("status") in ("shortlisted", "selected"):
                        qc = s.get("quote_cost")
                        qs = s.get("quote_status", "unquoted")
                        curr = s.get("quote_currency")
                        prefix = "$" if (not curr or curr == "AUD") else f"{curr} "
                        price_str = f"{prefix}{float(qc):,.2f}" if qc is not None else "—"
                        status_str = qs if qs != "unquoted" else "awaiting"
                        sup_items.append({
                            "line": item.get("line", "?"),
                            "desc": (item.get("input_description") or "—")[:50],
                            "pn": item.get("part_number") or "—",
                            "ns_id": item.get("input_code") or "—",
                            "qty": item.get("quantity") or 0,
                            "price": price_str,
                            "status": status_str,
                        })
                        break

            if sup_items:
                lines.append("")
                lines.append("| Line | Item | Part # | NS ID | Qty | Price | Status |")
                lines.append("|------|------|--------|-------|-----|-------|--------|")
                for si in sup_items:
                    lines.append(f"| {si['line']} | {si['desc']} | {si['pn']} | {si['ns_id']} | {si['qty']} | {si['price']} | {si['status']} |")
            else:
                lines.append("\n*No items quoted for this supplier.*")

    return "\n".join(lines)


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
            model=Config.DEFAULT_MODEL,
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

    Strategy (optimised for speed):
    1. Match by name + country against DB first — no web lookups.
    2. If matched → enrich from DB (contacts, URL, currency, tier, category).
    3. If NOT matched → only THEN search the web for a URL, and create a new record.

    Mutates supplier dicts in-place.

    Args:
        suppliers: List of supplier dicts to match/create.
        product_hint: Optional product context (part number, brand, description)
                      used to improve URL search accuracy for NEW suppliers only.
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

    # --- Phase 1: Fast DB-only matching (no I/O) ---
    # Close session quickly to avoid holding connections during web searches below.
    session = get_session()
    matched: set[str] = set()  # track names already matched
    needs_web_search: dict[str, list[dict]] = {}  # names that need URL lookup
    try:
        for name_lower, sup_list in names_to_match.items():
            sup_country = sup_list[0].get("country")
            row = match_supplier(name_lower, url=None, country=sup_country, session=session)
            if row:
                logger.info(f"[supplier-match] '{name_lower}' → '{row.name}' (id={row.id})")
                matched.add(name_lower)
                for sup in sup_list:
                    sup["supplier_id"] = str(row.id)
                    if row.currency and not sup.get("currency"):
                        sup["currency"] = row.currency
                    if row.country and not sup.get("country"):
                        sup["country"] = row.country
                    if row.supply_chain_position:
                        scp = row.supply_chain_position
                        if scp.get("tier") and not sup.get("tier"):
                            sup["tier"] = scp["tier"]
                        if scp.get("category") and not sup.get("category"):
                            sup["category"] = scp["category"]
                    if row.contacts:
                        merge_supplier_contacts(sup, row.contacts)
            else:
                needs_web_search[name_lower] = sup_list
    finally:
        session.close()  # ← release connection before any I/O

    if not needs_web_search:
        return

    # --- Phase 2: Web searches (NO session held) ---
    web_results: dict[str, str | None] = {}  # name_lower → verified_url or None
    for name_lower, sup_list in needs_web_search.items():
        sup_url = None
        for c in sup_list[0].get("contacts", []):
            if isinstance(c, dict) and c.get("url"):
                sup_url = c["url"]
                break
        verified_url = _verify_supplier_url(
            sup_list[0].get("name", "").strip(),
            sup_url,
            sup_list[0].get("country"),
            product_hint=product_hint,
        )
        web_results[name_lower] = verified_url
        if verified_url and verified_url != sup_url:
            logger.info(f"[url-verify] Found URL for '{sup_list[0].get('name')}': {verified_url}")
            for sup in sup_list:
                for c in sup.get("contacts", []):
                    if isinstance(c, dict) and c.get("url") == sup_url:
                        c["url"] = verified_url

    # --- Phase 3: Retry match with URL + create new suppliers (fresh session) ---
    session = get_session()
    try:
        for name_lower, sup_list in needs_web_search.items():
            sup_url = web_results.get(name_lower)
            sup_country = sup_list[0].get("country")

            # Step 4: Retry match with URL
            if sup_url:
                row = match_supplier(name_lower, url=sup_url, country=sup_country, session=session)
                if row:
                    logger.info(f"[supplier-match] '{name_lower}' matched via URL → '{row.name}'")
                    for sup in sup_list:
                        sup["supplier_id"] = str(row.id)
                        if row.currency and not sup.get("currency"):
                            sup["currency"] = row.currency
                        if row.country and not sup.get("country"):
                            sup["country"] = row.country
                        if row.supply_chain_position:
                            scp = row.supply_chain_position
                            if scp.get("tier") and not sup.get("tier"):
                                sup["tier"] = scp["tier"]
                            if scp.get("category") and not sup.get("category"):
                                sup["category"] = scp["category"]
                        if row.contacts:
                            merge_supplier_contacts(sup, row.contacts)
                    continue

            # Step 5: Create new supplier
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
            session.flush()
            logger.info(f"[supplier-create] Created new web supplier '{new_supplier.name}' (id={new_supplier.id})")

            # Write contacts to Contact table
            imported_contacts = ref_sup.get("contacts") or []
            if imported_contacts and isinstance(imported_contacts, list):
                from includes.dashboard.models import Contact as ContactModel
                for c in imported_contacts:
                    if isinstance(c, dict) and (c.get("email") or c.get("name") or c.get("phone")):
                        session.add(ContactModel(
                            supplier_id=new_supplier.id,
                            label=c.get("label", "Main"),
                            fullname=c.get("name"),
                            email=c.get("email"),
                            phone=c.get("phone"),
                            isinactive=False,
                        ))

            # AI categorization — close session first, then reopen
            # (categorize_supplier makes its own Gemini API call)
            session.flush()
            cat_data = None
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
                    _client, Config.DEFAULT_MODEL, _taxonomy, _cat_input
                )
                new_supplier.supply_chain_position = {
                    "category": cat_result.get("category"),
                    "tier": cat_result.get("tier"),
                    "confidence": cat_result.get("confidence"),
                    "reasoning": cat_result.get("reasoning"),
                }
                new_supplier.modified_by = "ai:categorizer"
                cat_data = cat_result
                logger.info(
                    f"[supplier-categorize] '{new_supplier.name}' → "
                    f"{cat_result.get('tier')}/{cat_result.get('category')} "
                    f"(confidence={cat_result.get('confidence')})"
                )
            except Exception as cat_err:
                logger.warning(f"[supplier-categorize] Failed for '{new_supplier.name}': {cat_err}")
                if ref_sup.get("tier") and ref_sup.get("category"):
                    new_supplier.supply_chain_position = {
                        "tier": ref_sup["tier"],
                        "category": ref_sup["category"],
                    }

            for sup in sup_list:
                sup["supplier_id"] = str(new_supplier.id)
                if cat_data:
                    if cat_data.get("tier"):
                        sup["tier"] = cat_data["tier"]
                    if cat_data.get("category"):
                        sup["category"] = cat_data["category"]
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
    from includes.tools import rfq_crud as _crud
    session = _crud._get_session()
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


async def _notify_rfq_updated() -> None:
    """Notify the dashboard to refresh after RFQ data changes."""
    from includes.agent_bridge import notify_dashboard
    await notify_dashboard("dashboard_refresh")


async def _notify_agent_working(label: str) -> None:
    """Show the blue 'agent working' badge in the dashboard header."""
    from includes.agent_bridge import notify_dashboard
    await notify_dashboard("agent_working", {"label": label})


async def _stream_to_user(text: str) -> None:
    """Stream text to the user's active Chainlit message (if available)."""
    try:
        import chainlit as cl
        msg = cl.user_session.get("active_msg")
        if msg and text:
            await msg.stream_token(text)
    except Exception:
        pass  # Not in Chainlit context or no active message


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
                          brand, product_id, quantity, uom, match, notes.
                          Item match values: unmatched, specific, branded,
                          generic, discrepancy (problem found — part number
                          mismatch or cannot be verified)
          delete_item   — Delete a line item from the RFQ. data keys: line
                          (required, int). IMPORTANT: Always confirm with the
                          user before deleting. Remaining items are renumbered.
          add_items     — Add multiple line items to an existing RFQ. data keys:
                          items (required, list of dicts with input_description,
                          input_code, part_number, brand, quantity, uom).
                          Use this instead of create when the RFQ already exists.
          add_supplier  — Add supplier candidate(s) to a line item. data keys:
                          line (required), EITHER name (required) for a single
                          supplier with optional supplier_id, contacts, status,
                          price, price_type, currency, lead_time, notes,
                          purchase_ref; OR suppliers (list of dicts with those
                          same keys) to add multiple at once.
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
                          currency: 3-letter ISO code for the price currency
                          (e.g. 'AUD', 'USD', 'EUR', 'GBP'). Required when
                          price is not in AUD. Omit or use 'AUD' for
                          Australian dollar prices.
                          purchase_ref: optional dict {doc_number, date,
                          order_count} linking to the latest purchase record.
          update_supplier — Update a supplier on a line item. data keys:
                          line (required), name (required), plus any of: status,
                          price, price_type, currency, lead_time, notes,
                          contacts, purchase_ref, quote_cost, quote_status,
                          quote_currency, quote_leadtime.
          update_quote  — Update quotation fields on a supplier×item. data keys:
                          line (required), name (required), plus any of:
                          quote_cost (float), quote_status (unquoted/quoted/
                          declined/selected), quote_currency (3-letter ISO),
                          quote_leadtime (e.g. '2 weeks').
          select_quote  — Mark a supplier as selected on a line item. Auto-
                          deselects any previous selection and copies the
                          quote_cost to the item's cost_price. Toggles off
                          if already selected. data keys: line (required),
                          name (required).
          decline_quote — Mark a supplier as declined and clear their price.
                          data keys: line (required), name (required).
          set_supplier_meta — Set RFQ-level supplier metadata. data keys:
                          name (required), plus any of: shipping_cost (float),
                          shipping_currency (3-letter ISO), notes (str),
                          terms (str).
          clear_suppliers — Remove all suppliers from line item(s). data keys:
                          line (optional, int — if omitted clears ALL lines)
          assign        — Reassign the RFQ. data keys: assigned_to (required)
          update_status — Change RFQ status. data keys: status (required, one of
                          draft/in_progress/awaiting_quotes/completed/cancelled)
          add_note      — Append a note. data keys: note (required)
          link_external — Set external IDs. data keys: netsuite_opportunity
                          and/or hubspot_deal
          group_items  — Set or update sourcing groups. data keys:
                          item_groups (required, the grouping result object
                          with {groups: [...], ungrouped: [...]})

        Args:
            action: The mutation to perform (see above).
            rfq_id: The RFQ identifier (required for all actions except create).
            data: Action-specific payload (see above).
        """
        data = data or {}

        # Gemini models sometimes pass data as a JSON string instead of a dict.
        if isinstance(data, str):
            import json
            logger.debug(f"manage_rfq: 'data' received as string, parsing JSON: {data[:200]}")
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
            "delete_item": lambda: asyncio.to_thread(_delete_item_sync, rfq_id, data.get("line"), user_id),
            "add_items": lambda: asyncio.to_thread(_add_items_sync, rfq_id, data, user_id),
            "add_supplier": lambda: asyncio.to_thread(_add_supplier_sync, rfq_id, data, user_id),
            "update_supplier": lambda: asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_id),
            "clear_suppliers": lambda: asyncio.to_thread(_clear_suppliers_sync, rfq_id, data, user_id),
            "assign": lambda: asyncio.to_thread(_assign_sync, rfq_id, data, user_id),
            "update_status": lambda: asyncio.to_thread(_update_status_sync, rfq_id, data, user_id),
            "add_note": lambda: asyncio.to_thread(_add_note_sync, rfq_id, data, user_id),
            "link_external": lambda: asyncio.to_thread(_link_external_sync, rfq_id, data, user_id),
            "group_items": lambda: asyncio.to_thread(_update_item_groups_sync, rfq_id, data.get("item_groups", data), user_id),
            "update_quote": lambda: asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_id),
            "select_quote": lambda: asyncio.to_thread(_select_quote_sync, rfq_id, data, user_id),
            "decline_quote": lambda: asyncio.to_thread(_decline_quote_sync, rfq_id, data, user_id),
            "set_supplier_meta": lambda: asyncio.to_thread(_set_supplier_meta_sync, rfq_id, data, user_id),
        }

        handler = _ACTION_MAP.get(action)
        if not handler:
            return (
                f"Error: unknown action '{action}'. Valid actions: create, "
                "update, update_item, delete_item, add_items, add_supplier, update_supplier, "
                "clear_suppliers, assign, update_status, add_note, link_external, "
                "group_items, update_quote, select_quote, decline_quote, set_supplier_meta."
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
        return _render_rfq_brief_summary(result)

    @tool
    async def get_rfq(
        rfq_id: Optional[str] = None,
        list_all: bool = False,
        assigned_to: Optional[str] = None,
        status: Optional[str] = None,
        brief: bool = True,
    ) -> str:
        """Retrieve RFQ details or list RFQs.

        Usage:
          get_rfq(rfq_id="RFQ-2026-0042")               — brief summary of one RFQ
          get_rfq(rfq_id="RFQ-2026-0042", brief=False)  — full detail (includes item/supplier tables)
          get_rfq(list_all=True)                          — summary list of all RFQs
          get_rfq(assigned_to="tom@eagle.com.au")         — RFQs assigned to a user
          get_rfq(status="in_progress")                   — filter by status

        Args:
            rfq_id: Specific RFQ identifier to retrieve.
            list_all: If True, return a summary of all RFQs.
            assigned_to: Filter RFQs by assignee email.
            status: Filter RFQs by status.
            brief: If True (default), return a concise one-line stats summary.
                   Set to False when the user explicitly asks for full item/supplier details.
        """
        if rfq_id:
            rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
            if not rfq:
                return f"RFQ '{rfq_id}' not found."
            await _notify_rfq_updated()
            if brief:
                return _render_rfq_brief_summary(rfq)
            return _render_rfq_summary(rfq)

        rfqs = await asyncio.to_thread(_list_rfqs_sync,
                                        assigned_to if assigned_to else None,
                                        status if status else None)

        if not list_all and not assigned_to and not status:
            rfqs = [r for r in rfqs if r.get("assigned_to") == user_id]
            if not rfqs:
                return "You have no RFQs assigned. Use `get_rfq(list_all=True)` to see all."

        return _render_rfq_list(rfqs)

    @tool
    async def classify_items(
        rfq_id: str,
        search_db: bool = True,
    ) -> str:
        """Classify all unmatched items on an RFQ by data completeness.

        Assigns a match level to every item that is still 'unmatched':
        - specific: has part_number + description (brand discoverable)
        - branded:  has brand + description (no part_number)
        - generic:  description only

        If search_db=True, also searches the internal product database for
        matching products on specific items.

        IMPORTANT: This MUST be called before finding suppliers. Items must
        be classified first so you know what kind of items you're dealing
        with. If the user asks to find suppliers and items are still
        unmatched, refuse politely and call this tool first.

        Returns a summary of what was classified and what still needs
        attention.
        """
        await _notify_agent_working("Classifying items...")
        from includes.tools.rfq_crud import _classify_rfq_items_sync

        result = await asyncio.to_thread(
            _classify_rfq_items_sync, rfq_id, user_id, search_db,
        )
        if isinstance(result, dict) and "error" in result:
            return result["error"]

        classified = result["classified"]
        db_matches = result["db_matches"]
        to_validate = result["to_validate"]
        unclassifiable = result.get("unclassifiable", [])

        total_items = sum(len(v) for v in classified.values()) + len(unclassifiable)
        if total_items == 0:
            rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
            item_count = len(rfq_dict.get("items", [])) if rfq_dict else 0
            return (
                f"All {item_count} items in {rfq_id} are already classified. "
                f"No unmatched items to process."
            )

        # ---- Build summary ----
        parts = [f"**Classification complete for {rfq_id}:**"]

        if classified["specific"]:
            parts.append(f"- 🟢 {len(classified['specific'])} specific (part number + description)")
        if classified["branded"]:
            parts.append(f"- 🔵 {len(classified['branded'])} branded (brand + description, no part number)")
        if classified["generic"]:
            parts.append(f"- 🟣 {len(classified['generic'])} generic (description only)")

        if db_matches:
            parts.append(f"\n**Found in product database:**")
            for line, pn, brand, _ in db_matches:
                parts.append(f"- line {line} → {pn} ({brand})")

        await _notify_rfq_updated()

        remaining_specific = len([i for i in to_validate
                                  if not any(i["line"] == m[0] for m in db_matches)])

        if unclassifiable:
            parts.append(
                f"\n⚠️ {len(unclassifiable)} item(s) have too little data to "
                f"classify. Add a description, part number, or brand."
            )

        if remaining_specific > 0:
            parts.append(
                f"\n⚠️ {remaining_specific} item(s) were NOT found in our product "
                f"database and will need web validation later:"
            )
            for item in to_validate:
                if not any(item["line"] == m[0] for m in db_matches):
                    parts.append(
                        f"- Line {item['line']}: {item.get('input_description', '')} | "
                        f"Part#: {item.get('part_number', '') or '—'} | "
                        f"Brand: {item.get('brand', '') or '—'}"
                    )
            parts.append(
                f"\nThese items will be validated when we search the web. "
                f"For now, proceed to group_items next."
            )
        else:
            parts.append(
                f"\n✅ All specific items matched in our product database. "
                f"No web validation needed. Proceed to group_items next."
            )

        return "\n".join(parts)

    @tool
    async def validate_items(rfq_id: str) -> str:
        """Validate specific items via web search for discrepancy detection.

        Call this after classify_items(). If any specific items were NOT
        found in the internal product database, this tool lists them for
        web validation.

        When items need validation, tell the user "I need to validate
        these items via web search — one moment." The system will
        automatically route the validation to the ResearchAgent.
        """
        await _notify_agent_working("Checking validation status...")

        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            return f"RFQ '{rfq_id}' not found."

        items = rfq_dict.get("items", [])
        to_validate = [
            i for i in items
            if i.get("match") == "specific" and not i.get("product_id")
        ]

        if not to_validate:
            return "All specific items are matched in the product database. No web validation needed."

        parts = [
            f"{len(to_validate)} item(s) in {rfq_id} need web validation. "
            f"Tell the user you're sending these to the ResearchAgent for "
            f"verification, then the system will handle the delegation.\n",
        ]
        for item in to_validate:
            parts.append(
                f"Line {item['line']}: {item.get('input_description', '')[:80]}  |  "
                f"Part#: {item.get('part_number', '') or '—'}  |  "
                f"Brand: {item.get('brand', '') or '—'}"
            )
        return "\n".join(parts)

    @tool
    async def find_previous_suppliers(rfq_id: str) -> str:
        """DEPRECATED — use the supplier search gate instead.

        This tool is no longer available. The supplier search process now
        goes through the interactive gate which presents 4 search options.
        Tell the user to say 'find suppliers' to open the supplier search gate,
        or click the supplier search buttons on the RFQ dashboard.
        """
        return (
            "This tool has been replaced by the supplier search gate. "
            "Tell the user to type 'find suppliers' to see the available options, "
            "or click the 'Find Previous Sales' button on the RFQ dashboard."
        )

    @tool
    async def group_items(rfq_id: str) -> str:
        """Group specific items on an RFQ by brand or supply chain.

        Uses AI to identify natural groupings — items that share a brand
        or supply chain and should be sourced together. Saves the groups
        to the RFQ.

        This helps organise the RFQ before finding suppliers and can
        reduce duplicate supplier searches across related items.
        """
        await _notify_agent_working("Grouping items...")
        from includes.tools.rfq_crud import _group_rfq_items_sync

        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            return f"RFQ '{rfq_id}' not found."

        specific_items = [
            {
                "line": i["line"],
                "input_description": i.get("input_description", ""),
                "part_number": i.get("part_number", ""),
                "brand": i.get("brand", ""),
            }
            for i in rfq_dict.get("items", [])
            if i.get("match") == "specific"
        ]

        if len(specific_items) < 2:
            return (
                f"Need at least 2 specific items to form groups. "
                f"{rfq_id} has {len(specific_items)} specific item(s)."
            )

        result = await asyncio.to_thread(
            _group_rfq_items_sync, rfq_id, specific_items, user_id,
        )
        await _notify_rfq_updated()
        if isinstance(result, dict) and "error" in result:
            return result["error"]

        groups = result.get("groups", [])
        ungrouped = result.get("ungrouped", [])

        ungrouped_reason = result.get("ungrouped_reason", "")

        if groups:
            group_summary = ", ".join(
                f"{g.get('label', '?')} (lines {', '.join(str(l) for l in g.get('lines', []))})"
                for g in groups
            )
            parts = [f"Grouped into {len(groups)} sourcing group(s): {group_summary}."]
        else:
            parts = ["No natural groupings were found."]

        if ungrouped:
            parts.append(
                f"{len(ungrouped)} item(s) remain ungrouped (lines "
                f"{', '.join(str(l) for l in ungrouped)})."
            )
            if ungrouped_reason:
                parts.append(f"Reason: {ungrouped_reason}")

        parts.append("Proceed to find previous suppliers.")

        return " ".join(parts)

    @tool
    async def view_rfq_quotation(rfq_id: str) -> str:
        """Get a comprehensive Markdown snapshot of an RFQ's quotation state.

        Returns a dual-view report:
        1. Item×Supplier price matrix — all items and their quoted prices from
           each shortlisted supplier, with selection status (★ selected, ✗ declined).
        2. Per-supplier detail sections — shipping cost, notes, terms, and the
           full list of items each supplier was asked to quote on.

        Includes NetSuite internal IDs, customer contact info, totals, and
        external reference IDs. Use this before any quotation work to get
        the full picture in a single call.

        Args:
            rfq_id: The RFQ identifier (e.g. 'RFQ-2026-0039').
        """
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return f"RFQ '{rfq_id}' not found."
        return _build_quotation_snapshot(rfq)

    return [manage_rfq, get_rfq, classify_items, validate_items, group_items, view_rfq_quotation]
