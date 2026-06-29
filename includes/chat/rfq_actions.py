"""Chainlit action callbacks for RFQ operations.

These handle button clicks from the RFQ dashboard custom elements
(Identify Items, Find Suppliers, Refresh, Update Supplier Status).
"""

import asyncio
import contextlib
import logging

import chainlit as cl

from includes.agent_bridge import notify_dashboard
from includes.tools.quote_tools import (
    _update_supplier_sync, _update_item_sync, _add_supplier_sync,
    _clear_suppliers_sync, _get_rfq_dict_sync,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thread-pinning utility
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _pin_thread():
    """Pin the current thread_id for the duration of a long-running callback.

    When a user navigates to a different RFQ while an action callback is
    processing, Chainlit's on_chat_resume overwrites the session thread_id.
    This causes in-flight callbacks to send messages (and graph updates)
    to the wrong thread.

    This context manager captures the thread_id at callback start. Use
    _send_pinned() and _main_pinned() within the block to ensure messages
    and graph invocations target the correct thread.

    Usage::

        async with _pin_thread() as thread_id:
            await _send_pinned("...", thread_id, author="EagleAgent")
            await _main_pinned(synthetic, thread_id)
    """
    session = cl.context.session
    pinned_thread_id = session.thread_id
    yield pinned_thread_id


@contextlib.asynccontextmanager
async def _thread_swap(target_thread_id: str):
    """Temporarily swap the session thread_id to target, restore after."""
    session = cl.context.session
    prev_user_thread = cl.user_session.get("thread_id")
    prev_session_thread = session.thread_id

    cl.user_session.set("thread_id", target_thread_id)
    session.thread_id = target_thread_id
    try:
        yield
    finally:
        cl.user_session.set("thread_id", prev_user_thread)
        session.thread_id = prev_session_thread


async def _send_pinned(content: str, pinned_thread_id: str, **kwargs):
    """Send a cl.Message to the pinned thread, even if session switched."""
    current = cl.user_session.get("thread_id")
    if current == pinned_thread_id:
        await cl.Message(content=content, **kwargs).send()
    else:
        # Thread switched — temporarily pin back for this send
        async with _thread_swap(pinned_thread_id):
            await cl.Message(content=content, **kwargs).send()


async def _main_pinned(synthetic_msg, pinned_thread_id: str):
    """Call the main() handler ensuring graph uses the pinned thread."""
    from app import main
    current = cl.user_session.get("thread_id")
    if current == pinned_thread_id:
        await main(synthetic_msg)
    else:
        async with _thread_swap(pinned_thread_id):
            await main(synthetic_msg)


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


@cl.action_callback("rfq_refresh")
async def on_rfq_refresh(action: cl.Action) -> None:
    """Refresh the dashboard RFQ view with latest data."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    if not rfq_id:
        return

    await notify_dashboard("dashboard_refresh")


@cl.action_callback("rfq_update_supplier")
async def on_rfq_update_supplier(action: cl.Action) -> None:
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
async def on_rfq_identify_items(action: cl.Action) -> None:
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

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    items = payload.get("items", [])

    if not items:
        return

    async with _pin_thread() as pinned_tid:

        await _send_pinned(
            f"Classifying & validating {len(items)} item(s) in {rfq_id}...",
            pinned_tid, author="EagleAgent",
        )
        await notify_dashboard("agent_working", {"label": "AI classifying items..."})

        user_id = cl.user_session.get("user_id", "unknown")

        try:
            # ---- Step A: Classify ALL items ----
            classified = []     # (line, match) for all classified items
            to_validate = []    # items that are 'specific' and need validation
            classification_summary = []

            for ui_item in items:
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
                await _send_pinned(
                    f"Classified {len(classified)} item(s):\n" + "\n".join(classification_summary),
                    pinned_tid, author="EagleAgent",
                )
            await notify_dashboard("dashboard_refresh")

            # ---- Step B: Validate specific items ----
            if to_validate:
                validated = []    # items matched in internal DB
                need_web = []     # items needing web search for discrepancy check

                for ui_item in to_validate:
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
                    await _send_pinned(msg, pinned_tid, author="EagleAgent")
                    await notify_dashboard("dashboard_refresh")

                # Validate remaining specific items via web search (two-step grounded approach)
                if need_web:
                    from includes.tools.rfq_crud import _validate_items_sync

                    await _send_pinned(
                        f"Validating {len(need_web)} item(s) against web sources to check for discrepancies...",
                        pinned_tid, author="EagleAgent",
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
                        await _send_pinned(
                            "\n".join(lines_out),
                            pinned_tid, author="EagleAgent",
                        )
                    elif validation_result.get("error"):
                        await _send_pinned(
                            f"⚠️ Web validation failed: {validation_result['error'][:80]}",
                            pinned_tid, author="EagleAgent",
                        )
                    await notify_dashboard("dashboard_refresh")
            else:
                await _send_pinned(
                    "All items classified. Items without part numbers are ready for supplier search.",
                    pinned_tid, author="EagleAgent",
                )

        finally:
            await notify_dashboard("agent_done")



@cl.action_callback("rfq_find_suppliers")
async def on_rfq_find_suppliers(action: cl.Action) -> None:
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

    async with _pin_thread() as pinned_tid:
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

            # Notify user of Phase 1 results and ask before web search
            all_existing = list(existing or []) + [s["name"] for s in internal_suppliers]

            if internal_suppliers:
                names = ", ".join(s["name"] for s in internal_suppliers)
                await _send_pinned(
                    f"Found {len(internal_suppliers)} supplier(s) from our records for line {line}: {names}.",
                    pinned_tid, author="EagleAgent",
                )
            else:
                await _send_pinned(
                    f"No matching suppliers found in our records for line {line}.",
                    pinned_tid, author="EagleAgent",
                )

            # Present action buttons — let user decide whether to search the web
            search_web_action = cl.Action(
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
                description=f"Search the web for suppliers for line {line}",
            )
            no_thanks_action = cl.Action(
                name="rfq_dismiss",
                payload={},
                label="No thanks",
            )
            await _send_pinned(
                f"Would you like me to search the web for additional suppliers for line {line}?",
                pinned_tid,
                author="EagleAgent",
                actions=[search_web_action, no_thanks_action],
            )
        finally:
            await notify_dashboard("agent_done")


@cl.action_callback("rfq_find_web_suppliers_for_line")
async def on_rfq_find_web_suppliers_for_line(action: cl.Action) -> None:
    """Web search for a single line item — triggered by user clicking 'Search Web'.

    This is Phase 2 of the per-item supplier search, only invoked when the user
    explicitly confirms they want web results.
    """
    from includes.tools.rfq_crud import _web_search_suppliers_sync, _sort_rfq_suppliers_sync
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

    async with _pin_thread() as pinned_tid:
        await notify_dashboard("agent_working", {"label": f"Web searching for line {line}..."})

        try:
            suppliers = await asyncio.to_thread(
                _web_search_suppliers_sync,
                description=description,
                part_number=part_number,
                brand=brand,
                existing_suppliers=existing,
                quantity=f"{quantity} {uom}".strip(),
            )

            if suppliers:
                user_id = cl.user_session.get("user_id", "unknown")
                await asyncio.to_thread(
                    _add_supplier_sync, rfq_id,
                    {"line": line, "suppliers": suppliers},
                    user_id,
                )
                await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
                await notify_dashboard("dashboard_refresh")
                names = ", ".join(s["name"] for s in suppliers[:5])
                await _send_pinned(
                    f"✅ Found **{len(suppliers)}** web supplier(s) for line {line}: {names}",
                    pinned_tid, author="EagleAgent",
                )
            else:
                await _send_pinned(
                    f"No additional suppliers found on the web for line {line}.",
                    pinned_tid, author="EagleAgent",
                )
        finally:
            await notify_dashboard("agent_done")


@cl.action_callback("rfq_dismiss")
async def on_rfq_dismiss(action: cl.Action) -> None:
    """Dismiss/acknowledge an action prompt — no-op.
    Also sets pipeline stage to 'complete' if the RFQ was awaiting web search.
    """
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "")
    if rfq_id:
        await _set_pipeline_stage(rfq_id, "complete")


@cl.action_callback("rfq_pipeline_fix_part")
async def on_rfq_pipeline_fix_part(action: cl.Action) -> None:
    """Fix a discrepant part number. Only resumes pipeline after all fixes are applied."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "")
    user_id = payload.get("user_id") or cl.user_session.get("user_id", "unknown")
    line = payload.get("line")
    correct_pn = payload.get("correct_part_number", "")
    total_discrepancies = payload.get("total_discrepancies", 1)

    if not rfq_id or not line or not correct_pn:
        await cl.Message(content="Error: missing payload data.", author="EagleAgent").send()
        return

    # Update the part number on the RFQ item
    _update_item_sync(rfq_id, {"line": line, "part_number": correct_pn, "match": "specific"}, user_id)
    await notify_dashboard("dashboard_refresh")

    # Track how many fixes have been applied in this gate session
    fixes_key = f"pipeline_fixes_{rfq_id}"
    fixes_applied = cl.user_session.get(fixes_key, 0) + 1
    cl.user_session.set(fixes_key, fixes_applied)

    if fixes_applied >= total_discrepancies:
        # All discrepancies fixed — resume pipeline
        cl.user_session.set(fixes_key, 0)
        await cl.Message(
            content=f"✏️ Updated Line {line} part number to **{correct_pn}**. All fixes applied, continuing pipeline...",
            author="EagleAgent",
        ).send()
        await _resume_pipeline_from(rfq_id, user_id, "group")
    else:
        # More fixes pending — just confirm this one
        remaining = total_discrepancies - fixes_applied
        await cl.Message(
            content=f"✏️ Updated Line {line} part number to **{correct_pn}**. ({remaining} more fix(es) available above, or click Skip & Continue)",
            author="EagleAgent",
        ).send()


@cl.action_callback("rfq_pipeline_skip_validation")
async def on_rfq_pipeline_skip_validation(action: cl.Action) -> None:
    """Skip validation discrepancies and continue the pipeline."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "")
    user_id = payload.get("user_id") or cl.user_session.get("user_id", "unknown")

    if not rfq_id:
        await cl.Message(content="Error: no RFQ ID provided.", author="EagleAgent").send()
        return

    # Reset fixes counter
    cl.user_session.set(f"pipeline_fixes_{rfq_id}", 0)

    await cl.Message(
        content="⏭️ Skipping validation issues. Continuing pipeline...",
        author="EagleAgent",
    ).send()

    # Resume pipeline from grouping stage
    await _resume_pipeline_from(rfq_id, user_id, "group")


@cl.action_callback("rfq_pipeline_retry_validation")
async def on_rfq_pipeline_retry_validation(action: cl.Action) -> None:
    """Retry the validation stage of the pipeline."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "")
    user_id = payload.get("user_id") or cl.user_session.get("user_id", "unknown")

    if not rfq_id:
        await cl.Message(content="Error: no RFQ ID provided.", author="EagleAgent").send()
        return

    await cl.Message(
        content="🔄 Retrying validation...",
        author="EagleAgent",
    ).send()

    # Resume pipeline from validation stage
    await _resume_pipeline_from(rfq_id, user_id, "validate")


async def _resume_pipeline_from(rfq_id: str, user_id: str, start_stage: str) -> None:
    """Resume the RFQ pipeline from a given stage (used by gate callbacks)."""
    from includes.agents.procurement_agent import ProcurementAgent
    from langchain_google_genai import ChatGoogleGenerativeAI
    from config.settings import Config

    await notify_dashboard("agent_working", {"label": "Continuing pipeline..."})

    try:
        # Set up an active streaming message so _stream_to_user works
        # and pipeline output appears before gate buttons
        active_msg = cl.Message(content="", author="EagleAgent")
        await active_msg.send()
        cl.user_session.set("active_msg", active_msg)

        # Create a minimal ProcurementAgent instance to call the stage dispatcher
        model = ChatGoogleGenerativeAI(model=Config.DEFAULT_MODEL, temperature=0)
        agent = ProcurementAgent(model=model)
        state = {"user_id": user_id, "messages": []}

        result = await agent._run_pipeline_from_stage(
            start_stage, rfq_id, user_id, state, items_filter=None
        )

        # Finalise the streaming message
        await active_msg.update()
        cl.user_session.set("active_msg", None)

    except Exception as e:
        logger.exception(f"Error resuming pipeline for {rfq_id} from '{start_stage}'")
        await cl.Message(content=f"Error: {e}", author="EagleAgent").send()
    finally:
        await notify_dashboard("agent_done")


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


@cl.action_callback("rfq_pipeline_web_search")
async def on_rfq_pipeline_web_search(action: cl.Action) -> None:
    """Run batch web search for all items on an RFQ (triggered by pipeline button)."""
    from includes.tools.rfq_crud import (
        _get_rfq_dict_sync, _web_search_suppliers_sync,
        _add_supplier_sync, _sort_rfq_suppliers_sync, _get_session,
    )
    from includes.dashboard.models import RFQ, RFQItem

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "")
    user_id = payload.get("user_id") or cl.user_session.get("user_id", "unknown")
    pinned_tid = cl.user_session.get("thread_id")

    if not rfq_id:
        await cl.Message(content="Error: no RFQ ID provided.", author="EagleAgent").send()
        return

    await notify_dashboard("agent_working", {"label": "Preparing web search..."})

    try:
        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            await cl.Message(content=f"Error: {rfq_id} not found.", author="EagleAgent").send()
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
            await cl.Message(content="No items eligible for web search.", author="EagleAgent").send()
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
        await cl.Message(
            content=f"**Web Search** — Searching for new suppliers: **{total_searches}** search(es)...",
            author="EagleAgent",
        ).send()

        WEB_SEARCH_CONCURRENCY = 3
        sem = asyncio.Semaphore(WEB_SEARCH_CONCURRENCY)
        total_added = 0

        async def _run_search(label, lines, items_for_search):
            async with sem:
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

                if suppliers:
                    for line_num in lines:
                        await asyncio.to_thread(
                            _add_supplier_sync, rfq_id,
                            {"line": line_num, "suppliers": suppliers},
                            user_id,
                        )
                    names = [s["name"] for s in suppliers[:5]]
                    await cl.Message(
                        content=f"   ✓ **{label}** (line {', '.join(str(l) for l in lines)}): {len(suppliers)} supplier(s) — {', '.join(names)}",
                        author="EagleAgent",
                    ).send()
                    return len(suppliers)
                else:
                    await cl.Message(
                        content=f"   ✗ **{label}** (line {', '.join(str(l) for l in lines)}): No new suppliers found",
                        author="EagleAgent",
                    ).send()
                    return 0

        await notify_dashboard("agent_working", {
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
        await notify_dashboard("dashboard_refresh")

        await cl.Message(
            content=f"✅ Web search complete. Added **{total_added}** new supplier(s) total.",
            author="EagleAgent",
        ).send()

    except Exception as e:
        logger.exception(f"Error in pipeline web search for {rfq_id}")
        await cl.Message(content=f"Error during web search: {e}", author="EagleAgent").send()
    finally:
        await notify_dashboard("agent_done")


@cl.action_callback("rfq_group_items")
async def on_rfq_group_items(action: cl.Action) -> None:
    """Group confirmed RFQ items by brand/supply chain using LLM."""
    import json
    from includes.tools.rfq_crud import _group_rfq_items_sync

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    items = payload.get("items", [])

    if len(items) < 2:
        await cl.Message(
            content="Need at least 2 confirmed items to group.",
            author="EagleAgent",
        ).send()
        return

    await notify_dashboard("agent_working", {"label": "Grouping items..."})

    try:
        user_id = cl.user_session.get("user_id", "unknown")
        result = await asyncio.to_thread(
            _group_rfq_items_sync, rfq_id, items, user_id,
        )
        if isinstance(result, dict) and "error" in result:
            await cl.Message(content=result["error"], author="EagleAgent").send()
            return

        await notify_dashboard("dashboard_refresh")

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

            await cl.Message(
                content="\n".join(parts),
                author="EagleAgent",
            ).send()
    except Exception as e:
        logger.exception(f"Error grouping items for {rfq_id}")
        await cl.Message(
            content=f"Error grouping items: {e}",
            author="EagleAgent",
        ).send()
    finally:
        await notify_dashboard("agent_done")


@cl.action_callback("rfq_find_all_suppliers")
async def on_rfq_find_all_suppliers(action: cl.Action) -> None:
    """Route the batch Find All Suppliers button through the agent pipeline.

    Creates a synthetic user message and invokes the main graph handler.
    ProcurementAgent's _try_find_suppliers_pipeline picks it up and runs
    all steps (classify → validate → group → find previous → brand lookup
    → cross-apply), then asks the user before web search.

    This ensures the button produces identical results to typing
    "find suppliers" in chat.
    """
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")

    async with _pin_thread() as pinned_tid:
        synthetic = cl.Message(
            content=f"Find suppliers for all items on {rfq_id}"
        )
        synthetic.author = "User"
        await _main_pinned(synthetic, pinned_tid)


@cl.action_callback("rfq_find_previous_suppliers")
async def on_rfq_find_previous_suppliers(action: cl.Action) -> None:
    """Phase 1+2+2b+2.5: Grouping, internal DB search, brand lookup, cross-apply."""
    async with _pin_thread() as pinned_tid:
        await _phase_previous_suppliers(action.payload or {}, pinned_tid)


@cl.action_callback("rfq_add_brand_supplier")
async def on_rfq_add_brand_supplier(action: cl.Action) -> None:
    """Add a single brand-linked supplier to a line item from the modal."""
    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    line = payload.get("line")
    supplier = payload.get("supplier", {})
    if not rfq_id or not line or not supplier.get("name"):
        return
    user_id = cl.user_session.get("user_id", "unknown")
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
        user_id,
    )
    await notify_dashboard("dashboard_refresh")


@cl.action_callback("rfq_find_new_suppliers")
async def on_rfq_find_new_suppliers(action: cl.Action) -> None:
    """Phase 3: Web search via ResearchAgent."""
    async with _pin_thread() as pinned_tid:
        await _phase_new_suppliers(action.payload or {}, pinned_tid)


async def _phase_previous_suppliers(payload: dict, pinned_tid: str = None):
    """Phase 1 (grouping) + Phase 2 (DB search) + Phase 2.5 (cross-apply)."""
    import json
    from langchain_google_genai import ChatGoogleGenerativeAI
    from config import config
    from includes.tools.product_tools import _find_purchase_history_for_part
    from includes.tools.rfq_crud import _update_item_groups_sync
    from includes.prompts import load_prompt

    # If no pinned_tid passed, capture current (legacy path)
    if not pinned_tid:
        pinned_tid = cl.user_session.get("thread_id")

    rfq_id = payload.get("rfq_id", "???")
    confirmed_items = payload.get("items", [])

    if not confirmed_items:
        await _send_pinned(
            "No confirmed items to find suppliers for.",
            pinned_tid, author="EagleAgent",
        )
        return

    await notify_dashboard("agent_working", {"label": "Phase 1/2: Grouping items..."})

    try:
        # ================================================================
        # Phase 1: Item grouping (LLM) — identify brand/supply-chain groups
        # ================================================================
        await _send_pinned(
            f"**Phase 1/2** — Grouping {len(confirmed_items)} confirmed item(s) by brand/supply chain...",
            pinned_tid, author="EagleAgent",
        )

        groups_result = None
        if len(confirmed_items) >= 2:
            grouping_prompt = load_prompt("rfq_item_grouping")
            grouping_items = [
                {
                    "line": i["line"],
                    "input_description": i.get("description", ""),
                    "part_number": i.get("part_number", ""),
                    "brand": i.get("brand", ""),
                }
                for i in confirmed_items
            ]
            input_payload = json.dumps({
                "items": grouping_items,
                "existing_groups": None,
            }, indent=2)

            full_prompt = (
                f"{grouping_prompt}\n\n"
                f"---\n\n"
                f"## Your Task\n\n"
                f"Group the following items from **{rfq_id}**.\n\n"
                f"```json\n{input_payload}\n```\n\n"
                f"Return ONLY the JSON output as specified in section 3."
            )

            model = ChatGoogleGenerativeAI(
                model=config.get_agent_model("procurement"),
                temperature=0.2,
                max_output_tokens=4096,
            )

            try:
                response = await model.ainvoke(full_prompt)
                raw_text = response.content
                if isinstance(raw_text, list):
                    raw_text = "\n".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in raw_text
                    )
                cleaned = raw_text.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    cleaned = "\n".join(lines).strip()

                groups_result = json.loads(cleaned)

                # Save groups to DB
                user_id = cl.user_session.get("user_id", "unknown")
                await asyncio.to_thread(_update_item_groups_sync, rfq_id, groups_result, user_id)
                await notify_dashboard("dashboard_refresh")

                n_groups = len(groups_result.get("groups", []))
                n_ungrouped = len(groups_result.get("ungrouped", []))
                await _send_pinned(
                    f"Grouped into **{n_groups}** group(s), **{n_ungrouped}** ungrouped. Now checking our records...",
                    pinned_tid, author="EagleAgent",
                )
            except Exception as e:
                logger.warning(f"Grouping failed, treating all items as ungrouped: {e}")
                groups_result = None
        else:
            await _send_pinned(
                f"Only 1 confirmed item, skipping grouping. Now checking our records...",
                pinned_tid, author="EagleAgent",
            )

        # ================================================================
        # Phase 2: Internal DB search — batch across ALL confirmed items
        # ================================================================
        await notify_dashboard("agent_working", {"label": f"Phase 2/2: Checking records ({len(confirmed_items)} items)..."})
        await _send_pinned(
            f"**Phase 2/2** — Searching our internal records for {len(confirmed_items)} confirmed item(s)...",
            pinned_tid, author="EagleAgent",
        )

        total_internal = 0
        # Track suppliers found per line for cross-apply in Phase 2.5
        suppliers_by_line = {}  # line -> list of supplier dicts

        for item in confirmed_items:
            line = item.get("line")
            part_number = item.get("part_number", "")
            existing = item.get("existing_suppliers", [])
            existing_names_lower = {n.lower() for n in existing}
            internal_suppliers = []

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
                except Exception as e:
                    logger.warning(f"Phase 2 DB search failed for line {line}: {e}")

            # Add internal suppliers to the RFQ
            if internal_suppliers:
                user_id = cl.user_session.get("user_id", "unknown")
                await asyncio.to_thread(
                    _add_supplier_sync, rfq_id,
                    {"line": line, "suppliers": internal_suppliers},
                    user_id,
                )
                total_internal += len(internal_suppliers)

            suppliers_by_line[line] = internal_suppliers

        if total_internal > 0:
            await notify_dashboard("dashboard_refresh")
            await _send_pinned(
                f"Found **{total_internal}** supplier(s) from our records. Now checking brand links...",
                pinned_tid, author="EagleAgent",
            )
        else:
            await _send_pinned(
                f"No matching suppliers in our records. Checking brand links...",
                pinned_tid, author="EagleAgent",
            )

        # ================================================================
        # Phase 2b: Brand-linked supplier lookup
        # ================================================================
        from includes.tools.product_tools import _find_brand_suppliers_with_tier
        from includes.dashboard.models import RFQ, RFQItem
        from includes.tools.rfq_crud import _get_session
        from sqlalchemy.orm.attributes import flag_modified

        def _save_brand_suppliers(rfq_number, line_num, sups):
            sess = _get_session()
            try:
                rfq_obj = sess.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
                if not rfq_obj:
                    return
                item_obj = sess.query(RFQItem).filter(
                    RFQItem.rfq_id == rfq_obj.id, RFQItem.line == line_num
                ).first()
                if not item_obj:
                    return
                item_obj.brand_suppliers = sups
                flag_modified(item_obj, "brand_suppliers")
                sess.commit()
            except Exception:
                sess.rollback()
                raise
            finally:
                sess.close()

        total_brand = 0
        await notify_dashboard("agent_working", {"label": "Checking brand-linked suppliers..."})

        for item in confirmed_items:
            line = item.get("line")
            brand = (item.get("brand") or "").strip()

            # Skip items without a real brand
            if not brand or brand.lower() == "other":
                continue

            try:
                brand_sups = await asyncio.to_thread(_find_brand_suppliers_with_tier, brand)
            except Exception as e:
                logger.warning(f"Phase 2b brand lookup failed for line {line} brand={brand}: {e}")
                continue

            if not brand_sups:
                continue

            # Determine which suppliers are already on this line
            existing_on_line = {s["name"].lower() for s in suppliers_by_line.get(line, [])}
            item_ctx_existing = item.get("existing_suppliers", [])
            existing_on_line.update(n.lower() for n in item_ctx_existing)

            # Filter to only new suppliers (not already on the line)
            new_brand_sups = [s for s in brand_sups if s["name"].lower() not in existing_on_line]

            # Auto-add top 5 Tier A suppliers to the RFQ item
            tier_a = [s for s in new_brand_sups if s.get("tier") == "A"][:5]
            if tier_a:
                tier_a_entries = [
                    {
                        "supplier_id": s["supplier_id"],
                        "name": s["name"],
                        "contacts": s.get("contacts", []),
                        "status": "candidate",
                        "price_type": "brand_link",
                        "notes": f"Brand-linked supplier (Tier A, {s['transaction_count']} transactions)",
                    }
                    for s in tier_a
                ]
                user_id = cl.user_session.get("user_id", "unknown")
                await asyncio.to_thread(
                    _add_supplier_sync, rfq_id,
                    {"line": line, "suppliers": tier_a_entries},
                    user_id,
                )
                total_brand += len(tier_a)
                # Track them so cross-apply/dedup won't re-add
                for s in tier_a:
                    existing_on_line.add(s["name"].lower())
                    suppliers_by_line.setdefault(line, []).append({
                        "supplier_id": s["supplier_id"],
                        "name": s["name"],
                        "contacts": s.get("contacts", []),
                        "status": "candidate",
                        "price_type": "brand_link",
                    })

            # Store ALL brand suppliers (full list) for the modal reference
            await asyncio.to_thread(_save_brand_suppliers, rfq_id, line, brand_sups)

        if total_brand > 0:
            await notify_dashboard("dashboard_refresh")
            await _send_pinned(
                f"Added **{total_brand}** Tier A brand-linked supplier(s).",
                pinned_tid, author="EagleAgent",
            )

        # ================================================================
        # Phase 2.5: Cross-apply suppliers within groups
        # ================================================================
        total_cross = 0
        if groups_result and total_internal > 0:
            await notify_dashboard("agent_working", {"label": "Cross-applying within groups..."})
            items_by_line_ctx = {i["line"]: i for i in confirmed_items}
            for g in groups_result.get("groups", []):
                group_lines = g.get("lines", [])
                if len(group_lines) < 2:
                    continue

                # Collect all unique suppliers across the group (keyed by name)
                group_suppliers = {}  # name_lower -> supplier entry (clean)
                for gl in group_lines:
                    for sup in suppliers_by_line.get(gl, []):
                        key = sup["name"].lower()
                        if key not in group_suppliers:
                            group_suppliers[key] = {
                                "supplier_id": sup.get("supplier_id"),
                                "name": sup["name"],
                                "contacts": sup.get("contacts", []),
                                "status": "candidate",
                                "price_type": "candidate",
                                "notes": "Cross-applied from group (supplier has history with other items in this group)",
                            }

                if not group_suppliers:
                    continue

                # For each line in the group, add any suppliers it doesn't have yet
                for gl in group_lines:
                    existing_on_line = {s["name"].lower() for s in suppliers_by_line.get(gl, [])}
                    # Also include suppliers the line already had before this run
                    item_ctx = items_by_line_ctx.get(gl)
                    if item_ctx:
                        existing_on_line.update(n.lower() for n in item_ctx.get("existing_suppliers", []))

                    to_add = [
                        s for name_lower, s in group_suppliers.items()
                        if name_lower not in existing_on_line
                    ]
                    if to_add:
                        await asyncio.to_thread(
                            _cross_apply_suppliers_sync, rfq_id, gl, to_add,
                        )
                        total_cross += len(to_add)

            if total_cross > 0:
                await notify_dashboard("dashboard_refresh")
                await _send_pinned(
                    f"Cross-applied **{total_cross}** supplier(s) within groups.",
                    pinned_tid, author="EagleAgent",
                )
            else:
                await _send_pinned(
                    f"No additional cross-apply needed.",
                    pinned_tid, author="EagleAgent",
                )

        # Sort suppliers on all line items
        from includes.tools.rfq_crud import _sort_rfq_suppliers_sync
        await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
        await notify_dashboard("dashboard_refresh")

        await _send_pinned(
            f"✅ Previous supplier search complete. Found **{total_internal + total_brand + total_cross}** supplier(s) from our records.",
            pinned_tid, author="EagleAgent",
        )

    except Exception as e:
        logger.exception(f"Error in find previous suppliers for {rfq_id}")
        await _send_pinned(
            f"Error finding previous suppliers: {e}",
            pinned_tid, author="EagleAgent",
        )
    finally:
        await notify_dashboard("agent_done")


async def _phase_new_suppliers(payload: dict, pinned_tid: str = None):
    """Phase 3: Web search via genai.Client with Google Search grounding."""
    import json
    from includes.tools.rfq_crud import (
        _get_rfq_sync, _sort_rfq_suppliers_sync, _add_supplier_sync,
        _web_search_suppliers_sync, _get_session,
    )

    # If no pinned_tid passed, capture current (legacy path)
    if not pinned_tid:
        pinned_tid = cl.user_session.get("thread_id")

    rfq_id = payload.get("rfq_id", "???")
    confirmed_items = payload.get("items", [])
    user_id = cl.user_session.get("user_id", "unknown")

    if not confirmed_items:
        await _send_pinned(
            "No confirmed items to search for.",
            pinned_tid, author="EagleAgent",
        )
        return

    await notify_dashboard("agent_working", {"label": "Preparing web search..."})

    try:
        # Load groups from DB (saved during Find Previous Suppliers)
        from includes.dashboard.models import RFQ, RFQItem
        session = _get_session()
        try:
            rfq_obj = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            groups_result = rfq_obj.item_groups if rfq_obj else None
        finally:
            session.close()

        # Re-read current supplier state from DB so we know what's already there
        session = _get_session()
        try:
            rfq_obj = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            if not rfq_obj:
                await _send_pinned(f"RFQ {rfq_id} not found.", pinned_tid, author="EagleAgent")
                return
            db_items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq_obj.id).all()
            current_suppliers_by_line = {}
            for dbi in db_items:
                names = [s["name"] for s in (dbi.suppliers or []) if isinstance(s, dict)]
                current_suppliers_by_line[dbi.line] = names
        finally:
            session.close()

        # Build items_with_context using current DB state for existing suppliers
        items_with_context = []
        for item in confirmed_items:
            line = item.get("line")
            existing_from_db = current_suppliers_by_line.get(line, [])
            items_with_context.append({
                **item,
                "existing_suppliers": existing_from_db,
            })

        # ================================================================
        # Build search tasks: list of (label, lines, items_for_search)
        # ================================================================
        items_by_line = {i["line"]: i for i in items_with_context}
        search_tasks: list[tuple[str, list[int], list[dict]]] = []
        grouped_lines: set[int] = set()

        if groups_result:
            for g in groups_result.get("groups", []):
                group_lines = g.get("lines", [])
                grouped_lines.update(group_lines)
                group_items = [items_by_line[l] for l in group_lines if l in items_by_line]
                if not group_items:
                    continue
                search_tasks.append((
                    g["label"],
                    group_lines,
                    group_items,
                ))

            for line_num in groups_result.get("ungrouped", []):
                if line_num in items_by_line:
                    ui = items_by_line[line_num]
                    desc = ui.get("description", "") or f"Line {line_num}"
                    search_tasks.append((
                        desc[:60],
                        [line_num],
                        [ui],
                    ))

        # Any items not covered by groups
        for item in items_with_context:
            if item["line"] not in grouped_lines and not any(item["line"] in t[1] for t in search_tasks):
                desc = item.get("description", "") or f"Line {item['line']}"
                search_tasks.append((
                    desc[:60],
                    [item["line"]],
                    [item],
                ))

        total_searches = len(search_tasks)
        await _send_pinned(
            f"**Web Search** — Searching the web: **{total_searches}** search(es) to perform...",
            pinned_tid, author="EagleAgent",
        )

        total_added = 0
        WEB_SEARCH_CONCURRENCY = 3

        async def _run_search(idx, label, lines, items_for_search):
            """Run a single web search + save results. Returns count added."""
            primary = items_for_search[0]
            all_existing = set()
            for it in items_for_search:
                all_existing.update(it.get("existing_suppliers", []))

            suppliers = await asyncio.to_thread(
                _web_search_suppliers_sync,
                description=primary.get("description", ""),
                part_number=primary.get("part_number", ""),
                brand=primary.get("brand", ""),
                existing_suppliers=list(all_existing),
                quantity=f"{primary.get('quantity', '')} {primary.get('uom', '')}".strip(),
            )

            if suppliers:
                for line_num in lines:
                    await asyncio.to_thread(
                        _add_supplier_sync, rfq_id,
                        {"line": line_num, "suppliers": suppliers},
                        user_id,
                    )
                names = [s["name"] for s in suppliers[:5]]
                await _send_pinned(
                    f"   ✓ **{label}**: {len(suppliers)} supplier(s) — {', '.join(names)}",
                    pinned_tid, author="EagleAgent",
                )
                return len(suppliers)
            else:
                await _send_pinned(
                    f"   ✗ **{label}**: No new suppliers found.",
                    pinned_tid, author="EagleAgent",
                )
                return 0

        # Process in batches of WEB_SEARCH_CONCURRENCY
        sem = asyncio.Semaphore(WEB_SEARCH_CONCURRENCY)

        async def _bounded_search(idx, label, lines, items_for_search):
            async with sem:
                return await _run_search(idx, label, lines, items_for_search)

        # Show which searches are starting
        for idx, (label, lines, _items) in enumerate(search_tasks, 1):
            lines_str = ", ".join(str(l) for l in lines)
            await _send_pinned(
                f"🔍 Search {idx}/{total_searches}: **{label}** (lines {lines_str})",
                pinned_tid, author="EagleAgent",
            )

        await notify_dashboard("agent_working", {
            "label": f"Web searching {total_searches} items ({WEB_SEARCH_CONCURRENCY} concurrent)..."
        })

        # Launch all searches with bounded concurrency
        tasks = [
            _bounded_search(idx, label, lines, items_for_search)
            for idx, (label, lines, items_for_search) in enumerate(search_tasks, 1)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                label = search_tasks[i][0]
                logger.error(f"Web search failed for '{label}': {result}")
            elif isinstance(result, int):
                total_added += result

        await notify_dashboard("dashboard_refresh")

        # Sort suppliers on all line items
        await notify_dashboard("agent_working", {"label": "Sorting suppliers..."})
        await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
        await notify_dashboard("dashboard_refresh")

        await _send_pinned(
            f"✅ Web supplier search complete. Added **{total_added}** supplier(s) total.",
            pinned_tid, author="EagleAgent",
        )

    except Exception as e:
        logger.exception(f"Error in find new suppliers for {rfq_id}")
        await _send_pinned(
            f"Error finding new suppliers: {e}",
            pinned_tid, author="EagleAgent",
        )
    finally:
        await notify_dashboard("agent_done")
