"""
Supplier search tools for the ProcurementAgent.

These tools provide the agent with supplier discovery capabilities:
- classify_rfq_items: Classify + validate + group items (prerequisite)
- search_previous_suppliers: Search purchase history
- search_brand_suppliers: Find brand-linked suppliers
- search_web_suppliers: Web search for new suppliers (domestic or international)
- show_supplier_search_options: Display the search menu with action buttons
- mark_supplier_search_complete: Mark the RFQ as done with supplier search

Each tool returns a text summary for the agent to incorporate in its response.
Button callbacks in rfq_actions.py call the same underlying _run_* functions.
"""

import asyncio
import logging
from typing import Optional

import chainlit as cl
from langchain_core.tools import tool

from includes.tools.rfq_crud import (
    _get_rfq_dict_sync,
    _classify_rfq_items_sync,
    _group_rfq_items_sync,
    _validate_items_sync,
    _find_purchase_suppliers_sync,
    _find_brand_suppliers_sync,
    _cross_apply_suppliers_sync,
    _web_search_suppliers_sync,
    _add_supplier_sync,
    _sort_rfq_suppliers_sync,
)
from includes.tools.quote_tools import _notify_rfq_updated

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline stage helper
# ---------------------------------------------------------------------------

def _set_pipeline_stage_sync(rfq_id: str, stage: str) -> None:
    """Update pipeline_stage on an RFQ (status marker only)."""
    from includes.tools.rfq_crud import _get_session
    from includes.dashboard.models import RFQ

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


_BRAND_EXCLUSIONS = {"other", "n/a", "na", "none", "unknown", ""}


def get_viable_search_types(rfq_id: str, line_numbers: list[int] | None = None) -> dict:
    """Determine which search types are viable based on item data.

    Inspects items in scope and returns which search capabilities make sense.

    Args:
        rfq_id: The RFQ identifier.
        line_numbers: If set, only consider these lines. If None, all items.

    Returns:
        Dict with keys:
            has_part_number: bool - at least one item has a part number
            has_brand: bool - at least one item has a meaningful brand
            viable: list[str] - list of viable search type IDs:
                "SEARCH_PREVIOUS", "SEARCH_BRAND", "SEARCH_WEB_AU", "SEARCH_WEB_INTL"
    """
    rfq_dict = _get_rfq_dict_sync(rfq_id)
    if not rfq_dict:
        # Can't determine — return all as viable
        return {
            "has_part_number": True,
            "has_brand": True,
            "viable": ["SEARCH_PREVIOUS", "SEARCH_BRAND", "SEARCH_WEB_AU", "SEARCH_WEB_INTL"],
        }

    items = rfq_dict.get("items", [])
    if line_numbers:
        items = [i for i in items if i.get("line") in set(line_numbers)]

    has_part_number = any(
        (i.get("part_number") or "").strip()
        for i in items
    )
    has_brand = any(
        (i.get("brand") or "").strip().lower() not in _BRAND_EXCLUSIONS
        for i in items
    )

    viable = []
    if has_part_number:
        viable.append("SEARCH_PREVIOUS")
    if has_brand:
        viable.append("SEARCH_BRAND")
    viable.append("SEARCH_WEB_AU")
    viable.append("SEARCH_WEB_INTL")

    return {
        "has_part_number": has_part_number,
        "has_brand": has_brand,
        "viable": viable,
    }


# ---------------------------------------------------------------------------
# Core execution functions (called by both tools and button callbacks)
# ---------------------------------------------------------------------------

def run_classify_sync(rfq_id: str, user_id: str) -> str:
    """Classify, validate, and group items. Returns summary text."""
    # Check if already classified — skip re-running
    rfq_dict = _get_rfq_dict_sync(rfq_id)
    if not rfq_dict:
        return f"Error: RFQ '{rfq_id}' not found."

    items = rfq_dict.get("items", [])
    unmatched = [i for i in items if i.get("match") == "unmatched"]

    if not unmatched:
        # Already classified — report current state
        specific = [i for i in items if i.get("match") == "specific"]
        branded = [i for i in items if i.get("match") == "branded"]
        generic = [i for i in items if i.get("match") == "generic"]
        parts = []
        if specific:
            parts.append(f"{len(specific)} specific")
        if branded:
            parts.append(f"{len(branded)} branded")
        if generic:
            parts.append(f"{len(generic)} generic")
        if parts:
            return f"Items already classified: {', '.join(parts)}. Ready to search for suppliers."
        else:
            return "Items already processed. Ready to search for suppliers."

    # Step 1: Classify
    result = _classify_rfq_items_sync(rfq_id, user_id, search_db=True)
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"

    classified = result["classified"]
    db_matches = result["db_matches"]
    to_validate = result["to_validate"]
    unclassifiable = result.get("unclassifiable", [])

    total = sum(len(v) for v in classified.values())
    lines = [f"**Classification:** {total} items classified."]
    if classified["specific"]:
        lines.append(f"- {len(classified['specific'])} specific (part number + description)")
    if classified["branded"]:
        lines.append(f"- {len(classified['branded'])} branded (brand + description)")
    if classified["generic"]:
        lines.append(f"- {len(classified['generic'])} generic (description only)")
    if db_matches:
        lines.append(f"- {len(db_matches)} found in product database")
    if unclassifiable:
        lines.append(f"- {len(unclassifiable)} items could not be auto-classified (minimal data)")

    # Step 2: Validate items not in DB
    needs_validation = [
        i for i in to_validate
        if not any(i["line"] == m[0] for m in db_matches)
    ]
    if needs_validation:
        val_result = _validate_items_sync(rfq_id, needs_validation, user_id)
        validated = val_result.get("validated", [])
        if validated:
            lines.append(f"\n**Validation:** Checked {len(validated)} item(s) via web search.")
            for v in validated:
                icon = "✅" if v.get("status") == "confirmed" else "🟠"
                lines.append(f"- {icon} Line {v['line']}: {v.get('findings', '')}")
        elif val_result.get("error"):
            lines.append(f"\n**Validation:** ⚠️ Failed — {val_result['error'][:80]}")
    else:
        if total > 0:
            lines.append("\n**Validation:** All items found in product database.")

    # Step 3: Group (if 2+ specific/branded items)
    groupable = [
        {
            "line": i["line"],
            "input_description": i.get("input_description", ""),
            "part_number": i.get("part_number", ""),
            "brand": i.get("brand", ""),
        }
        for i in items
        if i.get("match") in ("specific", "branded")
    ]
    if len(groupable) >= 2:
        group_result = _group_rfq_items_sync(rfq_id, groupable, user_id)
        if isinstance(group_result, dict) and "error" not in group_result:
            groups = group_result.get("groups", [])
            if groups:
                lines.append(f"\n**Grouping:** {len(groups)} sourcing group(s) identified.")
            else:
                lines.append(f"\n**Grouping:** No natural groupings found.")
        elif isinstance(group_result, dict) and "error" in group_result:
            lines.append(f"\n**Grouping:** ⚠️ {group_result['error']}")
    else:
        lines.append(f"\n**Grouping:** Skipped ({len(groupable)} groupable item(s)).")

    # Always end with ready-to-search message
    lines.append("\nReady to search for suppliers. All search types work regardless of classification status.")

    _set_pipeline_stage_sync(rfq_id, "classified")
    return "\n".join(lines)


def run_previous_suppliers_sync(rfq_id: str, user_id: str, line_numbers: list[int] | None = None) -> str:
    """Search purchase history for suppliers. Returns summary text."""
    result = _find_purchase_suppliers_sync(rfq_id, user_id)
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"

    total = result["added"]
    by_line = result["by_line"]

    # Filter reporting to requested lines if scoped
    if line_numbers:
        by_line = {k: v for k, v in by_line.items() if k in line_numbers}
        total = sum(len(v) for v in by_line.values())

    if total == 0:
        text = "**Previous Sales:** No suppliers found in purchase history."
    else:
        lines = [f"**Previous Sales:** Added {total} supplier(s) from purchase history to the RFQ."]
        for line, names in sorted(by_line.items()):
            lines.append(f"- Line {line}: {', '.join(names)}")
        text = "\n".join(lines)

    # Cross-apply within groups
    cross = _cross_apply_suppliers_sync(rfq_id, user_id)
    if isinstance(cross, dict) and cross.get("added", 0) > 0:
        text += f"\n- Cross-applied {cross['added']} supplier(s) across groups."

    _sort_rfq_suppliers_sync(rfq_id)
    _set_pipeline_stage_sync(rfq_id, "searching")
    return text


def run_brand_suppliers_sync(rfq_id: str, user_id: str, line_numbers: list[int] | None = None) -> str:
    """Find brand-linked suppliers. Returns summary text."""
    result = _find_brand_suppliers_sync(rfq_id, user_id)
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"

    total = result["added"]
    by_line = result["by_line"]

    # Filter reporting to requested lines if scoped
    if line_numbers:
        by_line = {k: v for k, v in by_line.items() if k in line_numbers}
        total = sum(len(v) for v in by_line.values())

    if total == 0:
        text = "**Brand Suppliers:** No additional brand-linked suppliers found."
    else:
        lines = [f"**Brand Suppliers:** Added {total} brand-linked supplier(s) to the RFQ."]
        for line, names in sorted(by_line.items()):
            lines.append(f"- Line {line}: {', '.join(names)}")
        text = "\n".join(lines)

    # Cross-apply within groups
    cross = _cross_apply_suppliers_sync(rfq_id, user_id)
    if isinstance(cross, dict) and cross.get("added", 0) > 0:
        text += f"\n- Cross-applied {cross['added']} supplier(s) across groups."

    _sort_rfq_suppliers_sync(rfq_id)
    _set_pipeline_stage_sync(rfq_id, "searching")
    return text


def run_web_search_suppliers_sync(
    rfq_id: str, user_id: str,
    domestic_only: bool = True,
    line_numbers: list[int] | None = None,
) -> str:
    """Web search for new suppliers. Returns summary text."""
    rfq_dict = _get_rfq_dict_sync(rfq_id)
    if not rfq_dict:
        return f"Error: RFQ '{rfq_id}' not found."

    items = rfq_dict.get("items", [])
    # Filter to classified items, optionally scoped by line
    filter_set = set(line_numbers) if line_numbers else None
    search_items = [
        i for i in items
        if i.get("match") in ("specific", "branded", "generic")
        and (filter_set is None or i["line"] in filter_set)
    ]

    if not search_items:
        return "No classified items to search for."

    geo = "Australian" if domestic_only else "International"
    results_lines = [f"**{geo} Supplier Search:** Searching {len(search_items)} item(s)..."]
    total_added = 0

    for item in search_items:
        line = item["line"]
        existing = [s["name"] for s in item.get("suppliers", []) if isinstance(s, dict)]

        suppliers = _web_search_suppliers_sync(
            description=item.get("input_description", ""),
            part_number=item.get("part_number", ""),
            brand=item.get("brand", ""),
            existing_suppliers=existing,
            quantity=f"{item.get('quantity', '')} {item.get('uom', '')}".strip(),
            domestic_only=domestic_only,
        )

        if suppliers:
            _add_supplier_sync(rfq_id, {"line": line, "suppliers": suppliers}, user_id)
            names = [s["name"] for s in suppliers[:5]]
            results_lines.append(f"- Line {line}: {len(suppliers)} supplier(s) — {', '.join(names)}")
            total_added += len(suppliers)
        else:
            results_lines.append(f"- Line {line}: No new suppliers found")

    _sort_rfq_suppliers_sync(rfq_id)
    _set_pipeline_stage_sync(rfq_id, "searching")

    results_lines.append(f"\n**Total:** {total_added} new supplier(s) added to the RFQ as candidates.")
    return "\n".join(results_lines)


# ---------------------------------------------------------------------------
# LangGraph tools (thin wrappers around the _run_* functions)
# ---------------------------------------------------------------------------

def create_supplier_search_tools(user_id: str) -> list:
    """Create supplier search tools bound to a user. Returns list of tools."""

    @tool
    async def classify_rfq_items(rfq_id: str) -> str:
        """Classify, validate, and group items on an RFQ.

        This runs once to categorize items. It:
        1. Classifies items as specific/branded/generic based on available data
        2. Validates specific items against the product database and web
        3. Groups related items for efficient supplier matching

        If items are already classified, returns immediately without re-running.
        If some items cannot be classified (insufficient data), supplier searches
        can still proceed — classification does NOT block searching.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")

        Returns:
            Summary of classification results. Always ends with ready-to-search.
        """
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        await _notify_agent_working("Classifying items...")
        result = await asyncio.to_thread(run_classify_sync, rfq_id, user_id)
        await _notify_rfq_updated()
        return result

    @tool
    async def search_previous_suppliers(
        rfq_id: str,
        line_numbers: Optional[list[int]] = None,
    ) -> str:
        """Search purchase history for suppliers who've previously sold these parts.

        IMPORTANT: Only call this when the user has explicitly asked to search
        previous sales/purchase history. Never call autonomously.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")
            line_numbers: Optional list of specific line numbers to search.
                         If None, searches all classified items.

        Returns:
            Summary of suppliers found from purchase history.
        """
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        scope = f" (lines {line_numbers})" if line_numbers else ""
        await _notify_agent_working(f"Searching purchase history{scope}...")
        result = await asyncio.to_thread(
            run_previous_suppliers_sync, rfq_id, user_id, line_numbers
        )
        await _notify_rfq_updated()
        return result

    @tool
    async def search_brand_suppliers(
        rfq_id: str,
        line_numbers: Optional[list[int]] = None,
    ) -> str:
        """Find suppliers already linked to item brands in our database.

        IMPORTANT: Only call this when the user has explicitly asked to search
        brand suppliers. Never call autonomously.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")
            line_numbers: Optional list of specific line numbers to search.
                         If None, searches all items with brands.

        Returns:
            Summary of brand-linked suppliers found.
        """
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        scope = f" (lines {line_numbers})" if line_numbers else ""
        await _notify_agent_working(f"Finding brand-linked suppliers{scope}...")
        result = await asyncio.to_thread(
            run_brand_suppliers_sync, rfq_id, user_id, line_numbers
        )
        await _notify_rfq_updated()
        return result

    @tool
    async def search_web_suppliers(
        rfq_id: str,
        domestic_only: bool = True,
        line_numbers: Optional[list[int]] = None,
    ) -> str:
        """Search the web for new suppliers.

        IMPORTANT: Only call this when the user has explicitly asked for a web
        search (Australian or international). Never call autonomously. Web
        searches cost money.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")
            domestic_only: If True, search only for Australian suppliers.
                          If False, search globally (international).
            line_numbers: Optional list of specific line numbers to search.
                         If None, searches all classified items.

        Returns:
            Summary of web search results and suppliers added.
        """
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        geo = "Australian" if domestic_only else "international"
        scope = f" (lines {line_numbers})" if line_numbers else ""
        await _notify_agent_working(f"Searching web for {geo} suppliers{scope}...")
        result = await asyncio.to_thread(
            run_web_search_suppliers_sync, rfq_id, user_id, domestic_only, line_numbers
        )
        await _notify_rfq_updated()
        return result

    @tool
    async def show_supplier_search_options(rfq_id: str) -> str:
        """Display the supplier search menu with clickable action buttons.

        Shows 4 search options (Previous Sales, Brand Suppliers, Australian,
        International) plus a Done button. Call this after classification
        or after completing a search to show the user their options.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")

        Returns:
            Confirmation that the menu was displayed.
        """
        from includes.chat.supplier_search_gate import show_search_menu

        await show_search_menu(rfq_id, user_id)
        return "Supplier search options displayed. Waiting for user choice."

    @tool
    async def mark_supplier_search_complete(rfq_id: str) -> str:
        """Mark the supplier search as complete on this RFQ.

        Call when the user indicates they're done searching for suppliers.

        Args:
            rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042")

        Returns:
            Confirmation message.
        """
        from includes.tools.quote_tools import _notify_rfq_updated

        await asyncio.to_thread(_set_pipeline_stage_sync, rfq_id, "complete")
        await _notify_rfq_updated()
        return f"✅ Supplier search marked complete for {rfq_id}."

    return [
        classify_rfq_items,
        search_previous_suppliers,
        search_brand_suppliers,
        search_web_suppliers,
        show_supplier_search_options,
        mark_supplier_search_complete,
    ]
