"""Action handlers for RFQ operations.

These handle button clicks from the RFQ dashboard custom elements
(Identify Items, Find Suppliers, Refresh, Update Supplier Status).

Each handler takes ``(payload, ctx)`` and is registered by name in
``RFQ_ACTIONS`` at the bottom of this module. ``app.py`` adapts them onto
Chainlit's ``@cl.action_callback``; nothing here knows about Chainlit.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

from includes.agent_bridge import notify_dashboard
from includes.chat.context import ActionSpec, ChatContext
from includes.tools.quote_tools import (
    _update_supplier_sync, _update_item_sync, _add_supplier_sync,
    _clear_suppliers_sync, _get_rfq_dict_sync,
)

logger = logging.getLogger(__name__)

ActionHandler = Callable[[dict, ChatContext], Awaitable[None]]


def _user_id(payload: dict, ctx: ChatContext) -> str:
    """The acting user: the payload wins, then the session, then a placeholder."""
    return payload.get("user_id") or ctx.user_email or "unknown"


async def _handle_stop(ctx: ChatContext) -> None:
    """Send a stopped message and clean up when a stop is detected."""
    await ctx.say("⏹ *Stopped by user.*", author="EagleAgent")
    await ctx.notify_dashboard("agent_done")


def _cross_apply_suppliers_sync(rfq_number: str, line_num: int, suppliers: list[dict]) -> None:
    """Append suppliers directly to a line item's JSON, bypassing enrichment.

    Used by Phase 2.5 to cross-apply group suppliers without pulling
    incorrect transaction history for a different product.
    """
    from includes.dashboard.models import RFQ, RFQItem
    from includes.tools.rfq_crud import _get_session
    from sqlalchemy.orm.attributes import flag_modified

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return
        item = session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line_num
        ).first()
        if not item:
            return

        current = list(item.suppliers or [])
        existing_names = {s["name"].lower() for s in current}

        for sup in suppliers:
            if sup["name"].lower() not in existing_names:
                current.append({
                    "supplier_id": sup.get("supplier_id"),
                    "name": sup["name"],
                    "contacts": sup.get("contacts", []),
                    "status": sup.get("status", "candidate"),
                    "price_type": sup.get("price_type", "candidate"),
                    "notes": sup.get("notes", ""),
                })
                existing_names.add(sup["name"].lower())

        item.suppliers = current
        flag_modified(item, "suppliers")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def on_rfq_refresh(payload: dict, ctx: ChatContext) -> None:
    """Refresh the dashboard RFQ view with latest data."""
    if not payload.get("rfq_id"):
        return

    await ctx.notify_dashboard("dashboard_refresh")


async def on_rfq_update_supplier(payload: dict, ctx: ChatContext) -> None:
    """Handle supplier status change from dashboard."""
    rfq_id = payload.get("rfq_id")
    line = payload.get("line")
    supplier_name = payload.get("supplier_name")
    new_status = payload.get("status")

    if not all([rfq_id, line, supplier_name, new_status]):
        return

    await asyncio.to_thread(
        _update_supplier_sync, rfq_id,
        {"line": line, "name": supplier_name, "status": new_status},
        _user_id(payload, ctx),
    )

    await ctx.notify_dashboard("dashboard_refresh")

    status_label = new_status.replace("_", " ")
    await ctx.say(
        f"Updated **{supplier_name}** on line {line} of {rfq_id} → *{status_label}*",
        author="EagleAgent",
    )


async def on_rfq_identify_items(payload: dict, ctx: ChatContext) -> None:
    """Classify & validate RFQ items.

    Step A: CLASSIFY — assign a match level to every unmatched item based on
            available data (deterministic, no I/O).
            specific: part_number + brand + description
            branded:  brand + description (no part_number)
            generic:  description only

    Step B: VALIDATE (specific items only) — search internal product DB,
            then web search for discrepancy detection.
    """
    from includes.tools.product_tools import _find_product_by_code

    rfq_id = payload.get("rfq_id", "???")
    items = payload.get("items", [])

    if not items:
        return

    # Clear any stale stop flag from a previous run
    ctx.reset_cancel()

    await ctx.say(
        f"Classifying & validating {len(items)} item(s) in {rfq_id}...",
        author="EagleAgent",
    )
    await ctx.notify_dashboard("agent_working", {"label": "AI classifying items..."})

    user_id = _user_id(payload, ctx)

    try:
        # ---- Step A: Classify ALL items ----
        classified = []     # (line, match) for all classified items
        to_validate = []    # items that are 'specific' and need validation
        classification_summary = []

        for ui_item in items:
            if ctx.cancelled:
                await _handle_stop(ctx)
                return
            line = ui_item.get("line")
            description = ui_item.get("description", "")
            part_number = ui_item.get("part_number", "")
            brand = ui_item.get("brand", "")

            has_part = bool(part_number)
            has_brand = bool(brand and brand.strip().lower() not in ("other", "n/a", "na", "none", "unknown"))
            has_desc = bool(description)

            match = None
            # specific:  has a part_number + description (brand is discoverable)
            # branded:   has brand + description, no part_number
            # generic:   description only
            if has_part and has_desc:
                match = "specific"
            elif has_brand and has_desc:
                match = "branded"
            elif has_desc:
                match = "generic"

            if match:
                await asyncio.to_thread(
                    _update_item_sync, rfq_id,
                    {"line": line, "match": match},
                    user_id,
                )
                classified.append(line)
                if match == "specific":
                    to_validate.append(ui_item)
                classification_summary.append(f"  Line {line} → 🟢 {match}")

        if classification_summary:
            await ctx.say(
                f"Classified {len(classified)} item(s):\n" + "\n".join(classification_summary),
                author="EagleAgent",
            )
        await ctx.notify_dashboard("dashboard_refresh")

        # ---- Step B: Validate specific items ----
        if to_validate:
            validated = []    # items matched in internal DB
            need_web = []     # items needing web search for discrepancy check

            for ui_item in to_validate:
                if ctx.cancelled:
                    await _handle_stop(ctx)
                    return
                line = ui_item.get("line")
                part_number = ui_item.get("part_number", "")
                brand = ui_item.get("brand", "")

                product = None
                try:
                    product = await asyncio.to_thread(
                        _find_product_by_code, part_number, brand or None,
                    )
                except Exception as e:
                    logger.warning(f"Phase 1 product search failed for line {line}: {e}")

                if product:
                    validated.append({
                        "line": line,
                        "part_number": product["part_number"],
                        "brand": product["brand"],
                        "product_id": product["id"],
                    })
                else:
                    need_web.append(ui_item)

            # Update items matched in internal DB
            if validated:
                for v in validated:
                    await asyncio.to_thread(
                        _update_item_sync, rfq_id,
                        {"line": v["line"], "part_number": v["part_number"],
                         "brand": v["brand"], "product_id": v["product_id"],
                         "match": "specific"},
                        user_id,
                    )
                match_desc = ", ".join(
                    f"line {v['line']} → {v['part_number']} ({v['brand']})" for v in validated
                )
                msg = f"Found {len(validated)} item(s) in our product database: {match_desc}."
                if need_web:
                    msg += f" Checking {len(need_web)} remaining item(s) online for discrepancies..."
                await ctx.say(msg, author="EagleAgent")
                await ctx.notify_dashboard("dashboard_refresh")

            # Validate remaining specific items via web search (two-step grounded approach)
            if need_web:
                from includes.tools.rfq_crud import _validate_items_sync

                await ctx.say(
                    f"Validating {len(need_web)} item(s) against web sources to check for discrepancies...",
                    author="EagleAgent",
                )
                web_items = [
                    {
                        "line": item.get("line"),
                        "input_description": item.get("description", ""),
                        "part_number": item.get("part_number", ""),
                        "brand": item.get("brand", ""),
                    }
                    for item in need_web
                ]
                validation_result = await asyncio.to_thread(
                    _validate_items_sync, rfq_id, web_items, user_id
                )
                validated_web = validation_result.get("validated", [])
                if validated_web:
                    lines_out = []
                    for v in validated_web:
                        status_icon = "✅" if v.get("status") == "confirmed" else "🟠"
                        lines_out.append(f"  {status_icon} Line {v['line']}: {v.get('findings', '')}")
                        if v.get("correct_part_number") and v.get("status") == "discrepancy":
                            lines_out.append(f"    Correct part number: {v['correct_part_number']}")
                    await ctx.say("\n".join(lines_out), author="EagleAgent")
                elif validation_result.get("error"):
                    await ctx.say(
                        f"⚠️ Web validation failed: {validation_result['error'][:80]}",
                        author="EagleAgent",
                    )
                await ctx.notify_dashboard("dashboard_refresh")
        else:
            await ctx.say(
                "All items classified. Items without part numbers are ready for supplier search.",
                author="EagleAgent",
            )

        # ---- Step C: Auto-set the quote brand (deterministic majority) ----
        # Counts item brands; a strict majority that matches the brands
        # database exactly wins. Ties and non-DB brands are left for a human.
        if not ctx.cancelled:
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            quote_brand_result = await asyncio.to_thread(
                _set_quote_brand_from_items_sync, rfq_id, user_id
            )
            if quote_brand_result:
                await ctx.say(f"🏷️ {quote_brand_result}", author="EagleAgent")
            await ctx.notify_dashboard("dashboard_refresh")

    finally:
        await ctx.notify_dashboard("agent_done")



async def on_rfq_find_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Handle Find Suppliers button from RFQ custom element.

    Phase 1: Search internal DB for suppliers (purchase history + supplier DB).
             Add any found directly to the RFQ with supplier_id and purchase refs.
    Phase 2: Route to ResearchAgent for web-based supplier discovery,
             with full context of what was already found internally.
    """
    from includes.tools.product_tools import _find_purchase_history_for_part

    rfq_id = payload.get("rfq_id", "???")
    line = payload.get("line")
    description = payload.get("description", "")
    part_number = payload.get("part_number", "")
    brand = payload.get("brand", "")
    quantity = payload.get("quantity", "")
    uom = payload.get("uom", "ea")
    existing = payload.get("existing_suppliers", [])

    await ctx.notify_dashboard("agent_working", {"label": f"Finding suppliers for line {line}..."})

    try:
        # ---- Phase 1: Internal DB search ----
        existing_names_lower = {n.lower() for n in existing}
        internal_suppliers = []

        if part_number:
            try:
                ph_rows = await asyncio.to_thread(_find_purchase_history_for_part, part_number, 20)
                for row in ph_rows:
                    if row["name"].lower() not in existing_names_lower:
                        internal_suppliers.append({
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
                        existing_names_lower.add(row["name"].lower())
            except Exception as e:
                logger.warning(f"Phase 1 purchase history search failed: {e}")

        # Add internal suppliers to the RFQ via SQL
        if internal_suppliers:
            await asyncio.to_thread(
                _add_supplier_sync, rfq_id,
                {"line": line, "suppliers": internal_suppliers},
                _user_id(payload, ctx),
            )
            await ctx.notify_dashboard("dashboard_refresh")

        # Notify user of Phase 1 results and ask before web search
        all_existing = list(existing or []) + [s["name"] for s in internal_suppliers]

        if internal_suppliers:
            names = ", ".join(s["name"] for s in internal_suppliers)
            await ctx.say(
                f"Found {len(internal_suppliers)} supplier(s) from our records for line {line}: {names}.",
                author="EagleAgent",
            )
        else:
            await ctx.say(
                f"No matching suppliers found in our records for line {line}.",
                author="EagleAgent",
            )

        # Present action buttons — let user decide whether to search the web
        await ctx.say(
            f"Would you like me to search the web for additional suppliers for line {line}?",
            author="EagleAgent",
            actions=[
                ActionSpec(
                    name="rfq_find_web_suppliers_for_line",
                    payload={
                        "rfq_id": rfq_id,
                        "line": line,
                        "description": description,
                        "part_number": part_number,
                        "brand": brand,
                        "quantity": quantity,
                        "uom": uom,
                        "existing_suppliers": all_existing,
                    },
                    label="🔍 Search Web",
                    tooltip=f"Search the web for suppliers for line {line}",
                ),
                ActionSpec(name="rfq_dismiss", payload={}, label="No thanks"),
            ],
        )
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_find_web_suppliers_for_line(payload: dict, ctx: ChatContext) -> None:
    """Web search for a single line item — triggered by user clicking 'Search Web'.

    This is Phase 2 of the per-item supplier search, only invoked when the user
    explicitly confirms they want web results.
    """
    from includes.tools.rfq_crud import _web_search_suppliers_sync, _sort_rfq_suppliers_sync

    rfq_id = payload.get("rfq_id", "???")
    line = payload.get("line")
    description = payload.get("description", "")
    part_number = payload.get("part_number", "")
    brand = payload.get("brand", "")
    quantity = payload.get("quantity", "")
    uom = payload.get("uom", "ea")
    existing = payload.get("existing_suppliers", [])

    await ctx.notify_dashboard("agent_working", {"label": f"Web searching for line {line}..."})

    try:
        suppliers = await asyncio.to_thread(
            _web_search_suppliers_sync,
            description=description,
            part_number=part_number,
            brand=brand,
            existing_suppliers=existing,
            quantity=f"{quantity} {uom}".strip(),
        )

        if ctx.cancelled:
            await _handle_stop(ctx)
            return

        if suppliers:
            await asyncio.to_thread(
                _add_supplier_sync, rfq_id,
                {"line": line, "suppliers": suppliers},
                _user_id(payload, ctx),
            )
            await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
            await ctx.notify_dashboard("dashboard_refresh")
            names = ", ".join(s["name"] for s in suppliers[:5])
            await ctx.say(
                f"✅ Found **{len(suppliers)}** web supplier(s) for line {line}: {names}",
                author="EagleAgent",
            )
        else:
            await ctx.say(
                f"No additional suppliers found on the web for line {line}.",
                author="EagleAgent",
            )
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_dismiss(payload: dict, ctx: ChatContext) -> None:
    """Dismiss/acknowledge an action prompt — no-op.
    Also sets pipeline stage to 'complete' if the RFQ was awaiting web search.
    """
    rfq_id = payload.get("rfq_id", "")
    if rfq_id:
        await _set_pipeline_stage(rfq_id, "complete")


async def on_rfq_pipeline_fix_part(payload: dict, ctx: ChatContext) -> None:
    """Fix a discrepant part number. Only resumes pipeline after all fixes are applied."""
    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)
    line = payload.get("line")
    correct_pn = payload.get("correct_part_number", "")
    total_discrepancies = payload.get("total_discrepancies", 1)

    if not rfq_id or not line or not correct_pn:
        await ctx.say("Error: missing payload data.", author="EagleAgent")
        return

    # Update the part number on the RFQ item
    _update_item_sync(rfq_id, {"line": line, "part_number": correct_pn, "match": "specific"}, user_id)
    await ctx.notify_dashboard("dashboard_refresh")

    # Track how many fixes have been applied in this gate session
    fixes_key = f"pipeline_fixes_{rfq_id}"
    fixes_applied = ctx.get(fixes_key, 0) + 1
    ctx.set(fixes_key, fixes_applied)

    if fixes_applied >= total_discrepancies:
        # All discrepancies fixed — resume pipeline
        ctx.set(fixes_key, 0)
        await ctx.say(
            f"✏️ Updated Line {line} part number to **{correct_pn}**. All fixes applied, continuing pipeline...",
            author="EagleAgent",
        )
        await _resume_pipeline_from(rfq_id, user_id, "group", ctx)
    else:
        # More fixes pending — just confirm this one
        remaining = total_discrepancies - fixes_applied
        await ctx.say(
            f"✏️ Updated Line {line} part number to **{correct_pn}**. ({remaining} more fix(es) available above, or click Skip & Continue)",
            author="EagleAgent",
        )


async def on_rfq_pipeline_skip_validation(payload: dict, ctx: ChatContext) -> None:
    """Skip validation discrepancies and continue the pipeline."""
    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)

    if not rfq_id:
        await ctx.say("Error: no RFQ ID provided.", author="EagleAgent")
        return

    # Reset fixes counter
    ctx.set(f"pipeline_fixes_{rfq_id}", 0)

    await ctx.say("⏭️ Skipping validation issues. Continuing pipeline...", author="EagleAgent")

    # Resume pipeline from grouping stage
    await _resume_pipeline_from(rfq_id, user_id, "group", ctx)


async def on_rfq_pipeline_retry_validation(payload: dict, ctx: ChatContext) -> None:
    """Retry the validation stage of the pipeline."""
    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)

    if not rfq_id:
        await ctx.say("Error: no RFQ ID provided.", author="EagleAgent")
        return

    await ctx.say("🔄 Retrying validation...", author="EagleAgent")

    # Resume pipeline from validation stage
    await _resume_pipeline_from(rfq_id, user_id, "validate", ctx)


async def _resume_pipeline_from(
    rfq_id: str, user_id: str, start_stage: str, ctx: ChatContext
) -> None:
    """Resume the RFQ pipeline from a given stage (used by gate callbacks)."""
    from includes.agents.procurement_agent import ProcurementAgent
    from langchain_google_genai import ChatGoogleGenerativeAI
    from config.settings import Config

    await ctx.notify_dashboard("agent_working", {"label": "Continuing pipeline..."})

    try:
        # An active streaming message so pipeline output appears before gate buttons
        active_msg = await ctx.say("", author="EagleAgent")
        ctx.active_message = active_msg

        # Create a minimal ProcurementAgent instance to call the stage dispatcher
        model = ChatGoogleGenerativeAI(model=Config.DEFAULT_MODEL, temperature=0)
        agent = ProcurementAgent(model=model)
        state = {"user_id": user_id, "messages": []}

        result = await agent._run_pipeline_from_stage(
            start_stage, rfq_id, user_id, state, items_filter=None
        )

        # Finalise the streaming message
        await active_msg.update()
        ctx.active_message = None

    except Exception as e:
        logger.exception(f"Error resuming pipeline for {rfq_id} from '{start_stage}'")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def _set_pipeline_stage(rfq_id: str, stage: str) -> None:
    """Helper to update pipeline_stage on an RFQ."""
    from includes.tools.rfq_crud import _get_session
    from includes.dashboard.models import RFQ

    def _update():
        session = _get_session()
        try:
            rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            if rfq:
                rfq.pipeline_stage = stage
                session.commit()
                logger.info(f"Pipeline[{rfq_id}]: stage → '{stage}'")
        except Exception as e:
            logger.error(f"Failed to update pipeline_stage for {rfq_id}: {e}")
            session.rollback()
        finally:
            session.close()

    await asyncio.to_thread(_update)


async def on_rfq_pipeline_web_search(payload: dict, ctx: ChatContext) -> None:
    """Run batch web search for all items on an RFQ (triggered by pipeline button)."""
    from includes.tools.rfq_crud import (
        _get_rfq_dict_sync, _web_search_suppliers_sync,
        _add_supplier_sync, _sort_rfq_suppliers_sync, _get_session,
    )
    from includes.dashboard.models import RFQ, RFQItem

    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)

    if not rfq_id:
        await ctx.say("Error: no RFQ ID provided.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Preparing web search..."})

    try:
        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            await ctx.say(f"Error: {rfq_id} not found.", author="EagleAgent")
            return

        items = rfq_dict.get("items", [])

        # Load groups and current suppliers from DB
        session = _get_session()
        try:
            rfq_obj = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            groups_result = rfq_obj.item_groups if rfq_obj else None
            db_items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq_obj.id).all() if rfq_obj else []
            current_suppliers_by_line = {}
            for dbi in db_items:
                names = [s["name"] for s in (dbi.suppliers or []) if isinstance(s, dict)]
                current_suppliers_by_line[dbi.line] = names
        finally:
            session.close()

        # Build items eligible for web search
        items_by_line = {}
        for item in items:
            line = item.get("line")
            if item.get("match") in ("specific", "branded", "generic"):
                items_by_line[line] = {
                    **item,
                    "existing_suppliers": current_suppliers_by_line.get(line, []),
                }

        if not items_by_line:
            await ctx.say("No items eligible for web search.", author="EagleAgent")
            return

        # Build search tasks (group-aware)
        search_tasks: list[tuple[str, list[int], list[dict]]] = []
        grouped_lines: set[int] = set()

        if groups_result:
            for g in groups_result.get("groups", []):
                group_lines = g.get("lines", [])
                grouped_lines.update(group_lines)
                group_items = [items_by_line[l] for l in group_lines if l in items_by_line]
                if group_items:
                    search_tasks.append((g["label"], group_lines, group_items))

            for line_num in groups_result.get("ungrouped", []):
                if line_num in items_by_line:
                    ui = items_by_line[line_num]
                    desc = ui.get("input_description", "") or f"Line {line_num}"
                    search_tasks.append((desc[:60], [line_num], [ui]))

        # Any items not covered by groups
        for line_num, item_ctx in items_by_line.items():
            if not any(line_num in t[1] for t in search_tasks):
                desc = item_ctx.get("input_description", "") or f"Line {line_num}"
                search_tasks.append((desc[:60], [line_num], [item_ctx]))

        total_searches = len(search_tasks)
        await ctx.say(
            f"**Web Search** — Searching for new suppliers: **{total_searches}** search(es)...",
            author="EagleAgent",
        )

        WEB_SEARCH_CONCURRENCY = 3
        sem = asyncio.Semaphore(WEB_SEARCH_CONCURRENCY)
        total_added = 0

        async def _run_search(label, lines, items_for_search):
            async with sem:
                if ctx.cancelled:
                    return 0
                primary = items_for_search[0]
                all_existing = set()
                for it in items_for_search:
                    all_existing.update(it.get("existing_suppliers", []))

                suppliers = await asyncio.to_thread(
                    _web_search_suppliers_sync,
                    description=primary.get("input_description", ""),
                    part_number=primary.get("part_number", ""),
                    brand=primary.get("brand", ""),
                    existing_suppliers=list(all_existing),
                    quantity=f"{primary.get('quantity', '')} {primary.get('uom', '')}".strip(),
                )

                if ctx.cancelled:
                    return 0

                if suppliers:
                    for line_num in lines:
                        if ctx.cancelled:
                            return 0
                        await asyncio.to_thread(
                            _add_supplier_sync, rfq_id,
                            {"line": line_num, "suppliers": suppliers},
                            user_id,
                        )
                        await ctx.notify_dashboard("dashboard_refresh")
                    names = [s["name"] for s in suppliers[:5]]
                    await ctx.say(
                        f"   ✓ **{label}** (line {', '.join(str(l) for l in lines)}): {len(suppliers)} supplier(s) — {', '.join(names)}",
                        author="EagleAgent",
                    )
                    return len(suppliers)
                else:
                    await ctx.say(
                        f"   ✗ **{label}** (line {', '.join(str(l) for l in lines)}): No new suppliers found",
                        author="EagleAgent",
                    )
                    return 0

        await ctx.notify_dashboard("agent_working", {
            "label": f"Web searching {total_searches} items ({WEB_SEARCH_CONCURRENCY} concurrent)..."
        })

        tasks = [
            _run_search(label, lines, items_for_search)
            for label, lines, items_for_search in search_tasks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Web search failed for '{search_tasks[i][0]}': {result}")
            elif isinstance(result, int):
                total_added += result

        # Sort suppliers and refresh dashboard
        await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
        await _set_pipeline_stage(rfq_id, "complete")
        await ctx.notify_dashboard("dashboard_refresh")

        await ctx.say(
            f"✅ Web search complete. Added **{total_added}** new supplier(s) total.",
            author="EagleAgent",
        )

    except Exception as e:
        logger.exception(f"Error in pipeline web search for {rfq_id}")
        await ctx.say(f"Error during web search: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


# ---------------------------------------------------------------------------
# Supplier search menu callbacks — triggered by the search option buttons.
# Each runs one search direction, then re-shows the menu so the user can
# pick another option or click Done.
# ---------------------------------------------------------------------------


async def on_rfq_pipeline_previous_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run purchase history search for classified items on the RFQ."""
    from includes.tools.supplier_search_tools import run_previous_suppliers_sync
    from includes.chat.supplier_search_gate import show_search_menu

    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)
    line_filter = payload.get("line_filter")

    if not rfq_id:
        await ctx.say("Error: no RFQ ID.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Searching purchase history..."})
    try:
        result = await asyncio.to_thread(run_previous_suppliers_sync, rfq_id, user_id, line_filter)
        await ctx.notify_dashboard("dashboard_refresh")
        await show_search_menu(rfq_id, user_id, summary=f"✅ {result}", line_filter=line_filter, ctx=ctx)
    except Exception as e:
        logger.exception(f"Error in previous suppliers for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_pipeline_brand_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run brand-linked supplier search for classified items on the RFQ."""
    from includes.tools.supplier_search_tools import run_brand_suppliers_sync
    from includes.chat.supplier_search_gate import show_search_menu

    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)
    line_filter = payload.get("line_filter")

    if not rfq_id:
        await ctx.say("Error: no RFQ ID.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Finding brand-linked suppliers..."})
    try:
        result = await asyncio.to_thread(run_brand_suppliers_sync, rfq_id, user_id, line_filter)
        await ctx.notify_dashboard("dashboard_refresh")
        await show_search_menu(rfq_id, user_id, summary=f"✅ {result}", line_filter=line_filter, ctx=ctx)
    except Exception as e:
        logger.exception(f"Error in brand suppliers for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_pipeline_new_domestic(payload: dict, ctx: ChatContext) -> None:
    """Run web search for new Australian (domestic) suppliers."""
    from includes.tools.supplier_search_tools import run_web_search_suppliers_sync
    from includes.chat.supplier_search_gate import show_search_menu

    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)
    line_filter = payload.get("line_filter")

    if not rfq_id:
        await ctx.say("Error: no RFQ ID.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Searching Australian suppliers..."})
    try:
        result = await asyncio.to_thread(
            run_web_search_suppliers_sync, rfq_id, user_id, True, line_filter
        )
        await ctx.notify_dashboard("dashboard_refresh")
        await show_search_menu(rfq_id, user_id, summary=f"✅ {result}", line_filter=line_filter, ctx=ctx)
    except Exception as e:
        logger.exception(f"Error in domestic web search for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_pipeline_new_international(payload: dict, ctx: ChatContext) -> None:
    """Run web search for new international suppliers."""
    from includes.tools.supplier_search_tools import run_web_search_suppliers_sync
    from includes.chat.supplier_search_gate import show_search_menu

    rfq_id = payload.get("rfq_id", "")
    user_id = _user_id(payload, ctx)
    line_filter = payload.get("line_filter")

    if not rfq_id:
        await ctx.say("Error: no RFQ ID.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Searching international suppliers..."})
    try:
        result = await asyncio.to_thread(
            run_web_search_suppliers_sync, rfq_id, user_id, False, line_filter
        )
        await ctx.notify_dashboard("dashboard_refresh")
        await show_search_menu(rfq_id, user_id, summary=f"✅ {result}", line_filter=line_filter, ctx=ctx)
    except Exception as e:
        logger.exception(f"Error in international web search for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_pipeline_supplier_search_done(payload: dict, ctx: ChatContext) -> None:
    """User clicked Done — mark supplier search complete."""
    from includes.tools.supplier_search_tools import _set_pipeline_stage_sync

    rfq_id = payload.get("rfq_id", "")

    if not rfq_id:
        await ctx.say("Error: no RFQ ID.", author="EagleAgent")
        return

    await asyncio.to_thread(_set_pipeline_stage_sync, rfq_id, "complete")
    await ctx.notify_dashboard("dashboard_refresh")
    await ctx.say(
        "✅ Supplier search complete. You can now review and manage suppliers on the RFQ dashboard.",
        author="EagleAgent",
    )
    await ctx.notify_dashboard("agent_done")


async def on_rfq_group_items(payload: dict, ctx: ChatContext) -> None:
    """Group confirmed RFQ items by brand/supply chain using LLM."""
    from includes.tools.rfq_crud import _group_rfq_items_sync

    rfq_id = payload.get("rfq_id", "???")
    items = payload.get("items", [])

    if len(items) < 2:
        await ctx.say("Need at least 2 confirmed items to group.", author="EagleAgent")
        return

    await ctx.notify_dashboard("agent_working", {"label": "Grouping items..."})

    try:
        result = await asyncio.to_thread(
            _group_rfq_items_sync, rfq_id, items, _user_id(payload, ctx),
        )
        if isinstance(result, dict) and "error" in result:
            await ctx.say(result["error"], author="EagleAgent")
            return

        await ctx.notify_dashboard("dashboard_refresh")

        groups = result.get("groups", [])
        ungrouped = result.get("ungrouped", [])

        parts = [f"**Item Grouping for {rfq_id}** — {len(groups)} group(s), {len(ungrouped)} ungrouped\n"]
        for g in groups:
            parts.append(f"### {g['id']}: {g['label']}")
            parts.append(f"Lines: {', '.join(str(l) for l in g['lines'])}")
            parts.append(f"*{g['reason']}*\n")

            if ungrouped:
                parts.append(f"### Ungrouped")
                parts.append(f"Lines: {', '.join(str(l) for l in ungrouped)}")
                if result.get("ungrouped_reason"):
                    parts.append(f"*{result['ungrouped_reason']}*")

            await ctx.say("\n".join(parts), author="EagleAgent")
    except Exception as e:
        logger.exception(f"Error grouping items for {rfq_id}")
        await ctx.say(f"Error grouping items: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_find_all_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run the batch Find All Suppliers request through the agent.

    The only handler that re-enters the graph. Dashboard-initiated, so it
    queues behind an active run rather than refusing.
    """
    from includes.chat.runner import run_turn

    rfq_id = payload.get("rfq_id", "???")
    await run_turn(
        f"Find suppliers for all items on {rfq_id}",
        ctx,
        graph=ctx.get("active_graph"),
        on_busy="wait",
    )


async def on_rfq_find_previous_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run previous suppliers search (legacy button)."""
    from includes.tools.supplier_search_tools import run_previous_suppliers_sync

    rfq_id = payload.get("rfq_id", "")
    if not rfq_id:
        return

    await ctx.notify_dashboard("agent_working", {"label": "Searching purchase history..."})
    try:
        result = await asyncio.to_thread(
            run_previous_suppliers_sync, rfq_id, _user_id(payload, ctx), None
        )
        await ctx.notify_dashboard("dashboard_refresh")
        await ctx.say(result, author="EagleAgent")
    except Exception as e:
        logger.exception(f"Error in previous suppliers for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_add_brand_supplier(payload: dict, ctx: ChatContext) -> None:
    """Add a single brand-linked supplier to a line item from the modal."""
    rfq_id = payload.get("rfq_id")
    line = payload.get("line")
    supplier = payload.get("supplier", {})
    if not rfq_id or not line or not supplier.get("name"):
        return
    sup_entry = {
        "supplier_id": supplier.get("supplier_id"),
        "name": supplier["name"],
        "contacts": supplier.get("contacts", []),
        "status": "candidate",
        "price_type": "brand_link",
        "notes": f"Brand-linked supplier (Tier {supplier.get('tier', '?')}, {supplier.get('transaction_count', 0)} transactions)",
    }
    await asyncio.to_thread(
        _add_supplier_sync, rfq_id,
        {"line": line, "suppliers": [sup_entry]},
        _user_id(payload, ctx),
    )
    await ctx.notify_dashboard("dashboard_refresh")


async def on_rfq_find_new_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run web search for new suppliers (legacy button)."""
    from includes.tools.supplier_search_tools import run_web_search_suppliers_sync

    rfq_id = payload.get("rfq_id", "")
    if not rfq_id:
        return

    await ctx.notify_dashboard("agent_working", {"label": "Searching web for suppliers..."})
    try:
        result = await asyncio.to_thread(
            run_web_search_suppliers_sync, rfq_id, _user_id(payload, ctx), True, None
        )
        await ctx.notify_dashboard("dashboard_refresh")
        await ctx.say(result, author="EagleAgent")
    except Exception as e:
        logger.exception(f"Error in web search for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


async def on_rfq_find_brand_suppliers(payload: dict, ctx: ChatContext) -> None:
    """Run brand-linked supplier search (legacy button)."""
    from includes.tools.supplier_search_tools import run_brand_suppliers_sync

    rfq_id = payload.get("rfq_id", "")
    if not rfq_id:
        return

    await ctx.notify_dashboard("agent_working", {"label": "Finding brand suppliers..."})
    try:
        result = await asyncio.to_thread(
            run_brand_suppliers_sync, rfq_id, _user_id(payload, ctx), None
        )
        await ctx.notify_dashboard("dashboard_refresh")
        await ctx.say(result, author="EagleAgent")
    except Exception as e:
        logger.exception(f"Error in brand suppliers for {rfq_id}")
        await ctx.say(f"Error: {e}", author="EagleAgent")
    finally:
        await ctx.notify_dashboard("agent_done")


# ---------------------------------------------------------------------------
# Registry — app.py adapts these onto Chainlit's @cl.action_callback
# ---------------------------------------------------------------------------

RFQ_ACTIONS: dict[str, ActionHandler] = {
    "rfq_refresh": on_rfq_refresh,
    "rfq_update_supplier": on_rfq_update_supplier,
    "rfq_identify_items": on_rfq_identify_items,
    "rfq_find_suppliers": on_rfq_find_suppliers,
    "rfq_find_web_suppliers_for_line": on_rfq_find_web_suppliers_for_line,
    "rfq_dismiss": on_rfq_dismiss,
    "rfq_pipeline_fix_part": on_rfq_pipeline_fix_part,
    "rfq_pipeline_skip_validation": on_rfq_pipeline_skip_validation,
    "rfq_pipeline_retry_validation": on_rfq_pipeline_retry_validation,
    "rfq_pipeline_web_search": on_rfq_pipeline_web_search,
    "rfq_pipeline_previous_suppliers": on_rfq_pipeline_previous_suppliers,
    "rfq_pipeline_brand_suppliers": on_rfq_pipeline_brand_suppliers,
    "rfq_pipeline_new_domestic": on_rfq_pipeline_new_domestic,
    "rfq_pipeline_new_international": on_rfq_pipeline_new_international,
    "rfq_pipeline_supplier_search_done": on_rfq_pipeline_supplier_search_done,
    "rfq_group_items": on_rfq_group_items,
    "rfq_find_all_suppliers": on_rfq_find_all_suppliers,
    "rfq_find_previous_suppliers": on_rfq_find_previous_suppliers,
    "rfq_add_brand_supplier": on_rfq_add_brand_supplier,
    "rfq_find_new_suppliers": on_rfq_find_new_suppliers,
    "rfq_find_brand_suppliers": on_rfq_find_brand_suppliers,
}


