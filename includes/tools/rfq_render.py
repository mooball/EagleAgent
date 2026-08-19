"""
RFQ rendering helpers — markdown formatters for RFQ summaries and lists.

All public names are re-exported from quote_tools.py for backward compatibility.
"""


def _render_rfq_summary(rfq: dict) -> str:
    """Render a single RFQ as a markdown summary block."""
    status_display = rfq.get("status", "draft").replace("_", " ").title()
    assigned = rfq.get("assigned_to", "Unassigned")
    customer = rfq.get("customer", "Unknown")
    created = rfq.get("created_date", "")
    rfq_id = rfq.get("id", "???")
    title = rfq.get("title", "")
    rfq_notes = rfq.get("notes", "")
    ref = rfq.get("reference", "")
    netsuite = rfq.get("netsuite_opportunity", "")
    hubspot = rfq.get("hubspot_deal", "")
    pipeline_stage = rfq.get("pipeline_stage", "")

    items = rfq.get("items", [])
    total = len(items)
    specific = sum(1 for i in items if i.get("match") == "specific")
    branded = sum(1 for i in items if i.get("match") == "branded")
    discrepancy = sum(1 for i in items if i.get("match") == "discrepancy")
    unmatched = sum(1 for i in items if i.get("match") == "unmatched")
    with_suppliers = sum(1 for i in items if i.get("suppliers"))

    lines = [f"## 📋 {rfq_id} — {customer}"]
    meta = [f"**Status:** {status_display}", f"**Assigned to:** {assigned}"]
    if created:
        meta.append(f"**Created:** {created}")
    lines.append(" | ".join(meta))

    if ref:
        lines.append(f"**Reference:** {ref}")
    quote_brand = rfq.get("quote_brand", "")
    ext_links = []
    if netsuite:
        ext_links.append(f"NetSuite: {netsuite}")
    if hubspot:
        ext_links.append(f"HubSpot: {hubspot}")
    if quote_brand:
        ext_links.append(f"Quote Brand: {quote_brand}")
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

    if title:
        lines.append(f"**Title:** {title}")
    if rfq_notes:
        lines.append(f"**Notes:** {rfq_notes}")
    if pipeline_stage:
        stage_label = pipeline_stage.replace("_", " ").title()
        lines.append(f"**Pipeline:** {stage_label}")

    lines.append("")

    if items:
        status_icons = {
            "specific": "🟢 Specific",
            "branded": "🔵 Branded",
            "discrepancy": "🟠 Discrepancy",
            "unmatched": "⬜ Unmatched",
        }
        lines.append("| # | Description | Part Number | Brand | Dept | Qty | Cost (AUD) | Sale | Match | Notes | Suppliers |")
        lines.append("|---|------------|-------------|-------|------|-----|------------|------|--------|-------|-----------|")
        for item in items:
            line_num = item.get("line", "")
            desc = item.get("input_description", "")
            pn = item.get("part_number") or item.get("input_code") or "—"
            brand = item.get("brand") or "—"
            dept = item.get("department") or "—"
            qty = item.get("quantity", "")
            uom = item.get("uom", "")
            qty_str = f"{qty} {uom}".strip() if qty else "—"
            status = status_icons.get(item.get("match", ""), item.get("match", ""))
            item_notes = item.get("notes", "") or "—"

            # Item-level cost/sale prices
            item_cost = item.get("cost_price")
            item_sale = item.get("sale_price")
            cost_str = f"${float(item_cost):,.2f}" if item_cost else "—"
            sale_str = f"${float(item_sale):,.2f}" if item_sale else "—"

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
                    cost_currency = s.get("cost_currency", "")
                    currency_sym = "$" if not cost_currency or cost_currency == "AUD" else ""
                    currency_tag = f" {cost_currency}" if cost_currency and cost_currency != "AUD" else ""
                    if st == "dropped":
                        sup_parts.append(f"~~{name}~~")
                    else:
                        parts = [name]
                        # Show cost/sale if available
                        if cost_price is not None or sale_price is not None:
                            price_bits = []
                            if cost_price is not None:
                                try:
                                    price_bits.append(f"cost {currency_sym}{float(cost_price):,.2f}{currency_tag}")
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
                                price_str = f"{currency_sym}{float(price):,.2f}{currency_tag}"
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
                f"| {line_num} | {desc} | {pn} | {brand} | {dept} | {qty_str} | {cost_str} | {sale_str} | {status} | {item_notes} | {sup_str} |"
            )

        lines.append("")
        counts = []
        if specific:
            counts.append(f"{specific} specific")
        if branded:
            counts.append(f"{branded} branded")
        if discrepancy:
            counts.append(f"{discrepancy} discrepancy")
        if unmatched:
            counts.append(f"{unmatched} unmatched")
        lines.append(
            f"**{total} items** | {', '.join(counts) if counts else 'none'} | "
            f"{with_suppliers} with suppliers"
        )

        # Render item groups if present
        item_groups = rfq.get("item_groups")
        if item_groups:
            groups = item_groups.get("groups", [])
            ungrouped = item_groups.get("ungrouped", [])
            if groups:
                lines.append("")
                lines.append(f"**Sourcing Groups** ({len(groups)}):")
                for g in groups:
                    line_list = ", ".join(str(l) for l in g.get("lines", []))
                    lines.append(f"- **{g.get('id', '?')}: {g.get('label', '?')}** — lines {line_list}")
                if ungrouped:
                    lines.append(f"- *Ungrouped:* lines {', '.join(str(l) for l in ungrouped)}")

        # Render supplier metadata (shipping, notes, terms per supplier)
        supplier_meta = rfq.get("supplier_meta") or {}
        if supplier_meta:
            lines.append("")
            lines.append("**Supplier Notes & Terms:**")
            for sup_name, meta in supplier_meta.items():
                if not isinstance(meta, dict):
                    continue
                bits = []
                shipping = meta.get("shipping_cost")
                shipping_curr = meta.get("shipping_currency", "")
                if shipping is not None:
                    try:
                        curr_tag = f" {shipping_curr}" if shipping_curr and shipping_curr != "AUD" else ""
                        bits.append(f"shipping ${float(shipping):,.2f}{curr_tag}")
                    except (ValueError, TypeError):
                        pass
                if meta.get("notes"):
                    bits.append(meta["notes"])
                if meta.get("terms"):
                    bits.append(f"terms: {meta['terms']}")
                if bits:
                    lines.append(f"- **{sup_name}:** {' · '.join(bits)}")
    else:
        lines.append("*No items yet.*")

    return "\n".join(lines)


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
        confirmed = sum(1 for i in items if i.get("match") == "specific")
        assigned = rfq.get("assigned_to", "—")
        created = rfq.get("created_date", "")
        lines.append(
            f"| [{rfq_id}](/rfqs/{rfq_id}) | {customer} | {status} | {confirmed}/{total} confirmed | {assigned} | {created} |"
        )
    lines.append("")
    lines.append(f"**{len(rfqs)} RFQs total**")

    return "\n".join(lines)


def _render_rfq_brief_summary(rfq: dict) -> str:
    """Render a concise one-line stats summary — no full item or supplier tables.

    Intended for tool responses after mutations (manage_rfq), where the full
    rendering would clutter the chat. The LLM gets enough context to write
    a brief natural-language update without being tempted to echo a big table.
    """
    rfq_id = rfq.get("id", "???")
    customer = rfq.get("customer", "Unknown")
    status = rfq.get("status", "draft").replace("_", " ").title()

    items = rfq.get("items", [])
    total = len(items)
    specific = sum(1 for i in items if i.get("match") == "specific")
    branded = sum(1 for i in items if i.get("match") == "branded")
    generic = sum(1 for i in items if i.get("match") == "generic")
    discrepancy = sum(1 for i in items if i.get("match") == "discrepancy")
    unmatched = sum(1 for i in items if i.get("match") == "unmatched")
    with_suppliers = sum(1 for i in items if i.get("suppliers"))

    counts = []
    if specific:
        counts.append(f"{specific} specific")
    if branded:
        counts.append(f"{branded} branded")
    if generic:
        counts.append(f"{generic} generic")
    if discrepancy:
        counts.append(f"{discrepancy} discrepancy")
    if unmatched:
        counts.append(f"{unmatched} unmatched")
    items_summary = f"{total} items: {', '.join(counts)}" if counts else f"{total} items"
    supplier_summary = f"{with_suppliers} line(s) have suppliers" if with_suppliers else "no suppliers yet"

    quote_brand = rfq.get("quote_brand") or ""
    brand_part = f" | Quote Brand: {quote_brand}" if quote_brand else ""
    return (
        f"RFQ {rfq_id} — {customer} ({status}) | "
        f"{items_summary} | {supplier_summary}{brand_part}"
    )
    return "\n".join(lines)
