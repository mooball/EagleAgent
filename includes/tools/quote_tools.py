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
    _list_rfqs_sync, _update_rfq_sync, _update_item_sync, _add_supplier_sync,
    _update_supplier_sync, _clear_suppliers_sync, _assign_sync,
    _update_status_sync, _add_note_sync, _link_external_sync,
)
from includes.tools.rfq_render import (  # noqa: F401
    _render_rfq_summary, _render_rfq_list,
)
from config.settings import Config

logger = logging.getLogger(__name__)


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
                        _client, Config.DEFAULT_MODEL, _taxonomy, _cat_input
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
                          contacts, purchase_ref
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
