"""Chainlit action callbacks for RFQ operations.

These handle button clicks from the RFQ dashboard custom elements
(Identify Items, Find Suppliers, Refresh, Update Supplier Status).
"""

import asyncio
import logging

import chainlit as cl

from includes.agent_bridge import notify_dashboard
from includes.tools.quote_tools import (
    _update_supplier_sync, _update_item_sync, _add_supplier_sync,
    _clear_suppliers_sync, _get_rfq_dict_sync,
)

logger = logging.getLogger(__name__)


@cl.action_callback("rfq_refresh")
async def on_rfq_refresh(action: cl.Action):
    """Refresh the dashboard RFQ view with latest data."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    if not rfq_id:
        return

    await notify_dashboard("dashboard_refresh")


@cl.action_callback("rfq_update_supplier")
async def on_rfq_update_supplier(action: cl.Action):
    """Handle supplier status change from dashboard."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    line = payload.get("line")
    supplier_name = payload.get("supplier_name")
    new_status = payload.get("status")

    if not all([rfq_id, line, supplier_name, new_status]):
        return

    user_id = cl.user_session.get("user_id", "unknown")
    await asyncio.to_thread(
        _update_supplier_sync, rfq_id,
        {"line": line, "name": supplier_name, "status": new_status},
        user_id,
    )

    # Refresh the dashboard view
    await notify_dashboard("dashboard_refresh")

    status_label = new_status.replace("_", " ")
    await cl.Message(
        content=f"Updated **{supplier_name}** on line {line} of {rfq_id} → *{status_label}*",
        author="EagleAgent",
    ).send()


@cl.action_callback("rfq_identify_items")
async def on_rfq_identify_items(action: cl.Action):
    """Handle Identify Items button from RFQ custom element.

    Phase 1: Search internal product DB by part number, supplier code,
             and description for each unidentified item.
             Update the RFQ directly for any exact matches (with product_id).
    Phase 2: Route unmatched items to ResearchAgent for web-based
             identification (must be 100% positive match).
    """
    from includes.tools.product_tools import _find_product_exact, _find_product_by_supplier_code

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    unidentified_items = payload.get("items", [])

    if not unidentified_items:
        return

    await cl.Message(
        content=f"Identifying {len(unidentified_items)} item(s) in {rfq_id}...",
        author="EagleAgent",
    ).send()
    await notify_dashboard("agent_working", {"label": "AI confirming items..."})

    try:
        # ---- Phase 1: Internal DB search ----
        matched = []      # list of dicts: line, part_number, brand, product_id
        unmatched = []     # items that need web search

        for ui_item in unidentified_items:
            line = ui_item.get("line")
            description = ui_item.get("description", "")
            part_number = ui_item.get("part_number", "")
            brand = ui_item.get("brand", "")

            product = None
            # Try exact part number match first (most specific)
            if part_number:
                try:
                    product = await asyncio.to_thread(
                        _find_product_exact, part_number, brand or None,
                    )
                except Exception as e:
                    logger.warning(f"Phase 1 product search failed for line {line}: {e}")

            # Try supplier code search if part number didn't match
            if not product and part_number:
                try:
                    product = await asyncio.to_thread(
                        _find_product_by_supplier_code, part_number, brand or None,
                    )
                except Exception as e:
                    logger.warning(f"Phase 1 supplier code search failed for line {line}: {e}")

            if product:
                matched.append({
                    "line": line,
                    "part_number": product["part_number"],
                    "brand": product["brand"],
                    "product_id": product["id"],
                })
            else:
                unmatched.append(ui_item)

        # Update RFQ with matched items via SQL
        if matched:
            user_id = cl.user_session.get("user_id", "unknown")
            for m in matched:
                await asyncio.to_thread(
                    _update_item_sync, rfq_id,
                    {"line": m["line"], "part_number": m["part_number"],
                     "brand": m["brand"], "product_id": m["product_id"],
                     "status": "confirmed"},
                    user_id,
                )
            await notify_dashboard("dashboard_refresh")

        # Notify user of Phase 1 results
        if matched:
            match_desc = ", ".join(f"line {m['line']} → {m['part_number']} ({m['brand']})" for m in matched)
            msg = f"Identified {len(matched)} item(s) from our product database: {match_desc}."
            if unmatched:
                msg += f" Searching the web for {len(unmatched)} remaining item(s)..."
            await cl.Message(content=msg, author="EagleAgent").send()
        elif unmatched:
            await cl.Message(
                content=f"No exact matches found in our product database for {len(unmatched)} item(s). Searching the web...",
                author="EagleAgent",
            ).send()

        # ---- Phase 2: Route unmatched items to ResearchAgent for web search ----
        if unmatched:
            parts = ["web_research"]
            parts.append(f"Identify the following unidentified product(s) from {rfq_id}.")
            parts.append("For each item, search the web to verify the part number and find a positive product match.")
            parts.append("")
            for ui_item in unmatched:
                line = ui_item.get("line")
                desc = ui_item.get("description", "")
                pn = ui_item.get("part_number", "")
                br = ui_item.get("brand", "")
                item_parts = [f"Line {line}: {desc}"]
                if pn:
                    item_parts.append(f"  Code/Part number: {pn}")
                if br:
                    item_parts.append(f"  Brand: {br}")
                parts.append("\n".join(item_parts))
            parts.append("")
            parts.append("IMPORTANT — Part number validation:")
            parts.append("For each item, search the web to verify BOTH that:")
            parts.append("  1. The part number actually exists as a real product")
            parts.append("  2. The product that part number refers to matches the given description")
            parts.append("For example, if the description says 'Hydraulic Return Filter' but the part number")
            parts.append("resolves to an oil filter or a completely different product, that is a mismatch.")
            parts.append("")
            parts.append("Flag an item for review (status='review') if ANY of these are true:")
            parts.append("- The exact part number cannot be found online")
            parts.append("- The part number exists but refers to a different product than the description")
            parts.append("- Similar/close part numbers exist that better match the description (possible typo)")
            parts.append("In review cases, add a notes field explaining the issue")
            parts.append("(e.g. 'Part number not found. Closest matches: 201-60-71180, 201-01-71110'")
            parts.append(" or 'Part number 600-211-2110 resolves to a fuel filter, not an oil filter as described').")
            parts.append("")
            parts.append("For each item:")
            parts.append("- EXACT match AND description matches: set part_number, brand, status='confirmed'")
            parts.append("- Part number wrong, missing, or mismatched to description: set status='review' and notes='...' explaining the issue. Do NOT clear or remove the existing part_number or brand — keep them as-is so the user can see what was originally provided.")
            parts.append("- Cannot identify at all: leave unchanged")
            parts.append("Do NOT set status='confirmed' unless you are 100% certain the part number is correct AND matches the description.")

            rich_prompt = "\n".join(parts)

            short_label = f"Identify {len(unmatched)} unmatched item(s) in {rfq_id} via web search"
            synthetic = cl.Message(content=short_label)
            synthetic.author = "User"
            synthetic.intent_context = rich_prompt

            # Import main handler lazily to avoid circular imports
            from app import main
            await main(synthetic)
        elif not matched:
            await cl.Message(
                content="All items could not be identified. Try adding more details (part numbers, brands) to help.",
                author="EagleAgent",
            ).send()
    finally:
        await notify_dashboard("agent_done")


@cl.action_callback("rfq_find_suppliers")
async def on_rfq_find_suppliers(action: cl.Action):
    """Handle Find Suppliers button from RFQ custom element.

    Phase 1: Search internal DB for suppliers (purchase history + supplier DB).
             Add any found directly to the RFQ with supplier_id and purchase refs.
    Phase 2: Route to ResearchAgent for web-based supplier discovery,
             with full context of what was already found internally.
    """
    from includes.tools.product_tools import _find_purchase_history_for_part

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    line = payload.get("line")
    description = payload.get("description", "")
    part_number = payload.get("part_number", "")
    brand = payload.get("brand", "")
    quantity = payload.get("quantity", "")
    uom = payload.get("uom", "ea")
    existing = payload.get("existing_suppliers", [])

    await notify_dashboard("agent_working", {"label": f"Finding suppliers for line {line}..."})

    try:
        # ---- Phase 1: Internal DB search ----
        existing_names_lower = {n.lower() for n in existing}
        internal_suppliers = []
        internal_summary_lines = []

        if part_number:
            try:
                ph_rows = await asyncio.to_thread(_find_purchase_history_for_part, part_number, 20)
                for row in ph_rows:
                    if row["name"].lower() not in existing_names_lower:
                        sup_entry = {
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
                        }
                        internal_suppliers.append(sup_entry)
                        existing_names_lower.add(row["name"].lower())
                        price_str = f"${row['price']:,.2f}" if row["price"] else "N/A"
                        internal_summary_lines.append(
                            f"- {row['name']} (previous purchase, price: {price_str}, orders: {row['order_count']})"
                        )
            except Exception as e:
                logger.warning(f"Phase 1 purchase history search failed: {e}")

        # Add internal suppliers to the RFQ via SQL
        if internal_suppliers:
            user_id = cl.user_session.get("user_id", "unknown")
            await asyncio.to_thread(
                _add_supplier_sync, rfq_id,
                {"line": line, "suppliers": internal_suppliers},
                user_id,
            )
            await notify_dashboard("dashboard_refresh")

        # Notify user of Phase 1 results
        if internal_suppliers:
            names = ", ".join(s["name"] for s in internal_suppliers)
            await cl.Message(
                content=f"Added {len(internal_suppliers)} supplier(s) from our records to line {line}: {names}. Now searching the web for more options...",
                author="EagleAgent",
            ).send()
        else:
            await cl.Message(
                content=f"No matching suppliers found in our records for line {line}. Searching the web...",
                author="EagleAgent",
            ).send()

        # ---- Phase 2: Route to ResearchAgent for web search ----
        all_existing = list(existing or []) + [s["name"] for s in internal_suppliers]

        parts = [f"research_suppliers"]
        parts.append(f"Find external suppliers for line {line} of {rfq_id}.")
        parts.append(f"Product description: {description}")
        if part_number:
            parts.append(f"Part number: {part_number}")
        if brand:
            parts.append(f"Brand: {brand}")
        if quantity:
            parts.append(f"Quantity needed: {quantity} {uom}")
        if all_existing:
            parts.append(f"Already have these suppliers (do NOT repeat them): {', '.join(all_existing)}")
        if internal_summary_lines:
            parts.append("Internal DB results:\n" + "\n".join(internal_summary_lines))
        parts.append("")
        parts.append("Search the web for distributors and wholesalers who can supply this product.")
        parts.append("")
        parts.append("## Geographic Priority")
        parts.append("1. FIRST search for Australian-based suppliers (add 'Australia' to your search queries).")
        parts.append("2. If fewer than 3 Australian suppliers are found, expand to international suppliers.")
        parts.append("3. When listing results, present Australian suppliers first, then international.")
        parts.append("")
        parts.append("## Supplier Selection")
        parts.append("Prioritise authorised distributors and industrial wholesalers over retail sources.")
        parts.append("If distributors are scarce, include reputable retailers as fallback options.")
        parts.append("Aim for 3-5 good supplier options but more is fine if they look like strong matches.")
        parts.append("")
        parts.append("## Supply Chain Classification")
        parts.append("Do NOT attempt to categorize suppliers yourself. The system will automatically classify each new supplier using our full taxonomy after you add them.")
        parts.append("You may optionally include 'tier' (A/B/C/D) and 'category' (e.g. 'Trade Wholesaler') if it is obvious, but the system will verify and correct these.")
        parts.append("")
        parts.append("CRITICAL: After researching, you MUST call manage_rfq(action='add_supplier') to add each supplier you find to the RFQ.")
        parts.append(f"Use rfq_id='{rfq_id}' and data={{line: {line}, suppliers: [...]}} with a list of all suppliers found.")
        parts.append("Each supplier dict must include: name, country (2-letter ISO code, e.g. 'AU', 'US', 'GB'), currency (3-letter ISO code for their trading currency, e.g. 'AUD', 'USD', 'GBP'), contacts (list with at least one of email/phone/url).")
        parts.append("Optional fields: tier, category, price, price_type, lead_time, notes.")
        parts.append("If a price is in a foreign currency, store the ORIGINAL price and set currency accordingly — do NOT convert to AUD.")
        parts.append("If you do NOT call add_supplier, the suppliers will NOT appear on the RFQ. The user is counting on you to update the RFQ directly.")

        rich_prompt = "\n".join(parts)

        short_label = f"Search the web for suppliers for line {line}"
        if description:
            short_label += f" ({description[:60]})"

        synthetic = cl.Message(content=short_label)
        synthetic.author = "User"
        synthetic.intent_context = rich_prompt

        # Track supplier count before web search to detect if any were added
        pre_web_count = 0
        _pre_rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if _pre_rfq:
            _pre_line = next((i for i in _pre_rfq.get("items", []) if i["line"] == line), None)
            if _pre_line:
                pre_web_count = len(_pre_line.get("suppliers", []))

        # Import main handler lazily to avoid circular imports
        from app import main
        await main(synthetic)

        # Fallback: if main() produced no visible response but suppliers were
        # added by the tool, notify the user so the result isn't silent.
        _post_rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if _post_rfq:
            _post_line = next((i for i in _post_rfq.get("items", []) if i["line"] == line), None)
            if _post_line:
                post_web_count = len(_post_line.get("suppliers", []))
                new_count = post_web_count - pre_web_count
                if new_count > 0:
                    new_suppliers = _post_line.get("suppliers", [])[-new_count:]
                    names = ", ".join(s.get("name", "?") for s in new_suppliers)
                    await cl.Message(
                        content=f"Web search complete — added {new_count} additional supplier(s) to line {line}: {names}.",
                        author="EagleAgent",
                    ).send()
                    await notify_dashboard("dashboard_refresh")
                elif not internal_suppliers:
                    await cl.Message(
                        content=f"Web search complete but no suitable suppliers found for line {line}. Try broadening the search terms or checking alternative part numbers.",
                        author="EagleAgent",
                    ).send()
    finally:
        await notify_dashboard("agent_done")
