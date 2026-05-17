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
        confirmed = sum(1 for i in items if i.get("status") == "confirmed")
        assigned = rfq.get("assigned_to", "—")
        created = rfq.get("created_date", "")
        lines.append(
            f"| [{rfq_id}](/rfqs/{rfq_id}) | {customer} | {status} | {confirmed}/{total} confirmed | {assigned} | {created} |"
        )
    lines.append("")
    lines.append(f"**{len(rfqs)} RFQs total**")
    return "\n".join(lines)
