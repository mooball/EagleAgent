"""
Supplier search menu — button builder and display helper.

Used by both the procurement agent (via show_supplier_search_options tool)
and the RFQ action callbacks to re-show the menu after each search completes.
"""

import logging

from includes.chat.context import ActionSpec, ChatContext, get_chat_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Menu UI builder
# ---------------------------------------------------------------------------

def build_menu_actions(
    rfq_id: str,
    user_id: str,
    line_filter: list[int] | None = None,
    viable_types: list[str] | None = None,
) -> list[ActionSpec]:
    """Build the action buttons for the supplier search menu.

    Args:
        rfq_id: The RFQ identifier.
        user_id: The current user.
        line_filter: If set, only operate on these line numbers.
        viable_types: If set, only include buttons for these search types.
            Valid values: "SEARCH_PREVIOUS", "SEARCH_BRAND", "SEARCH_WEB_AU", "SEARCH_WEB_INTL"
            If None, all buttons are shown.
    """
    base_payload = {"rfq_id": rfq_id, "user_id": user_id}
    if line_filter:
        base_payload["line_filter"] = line_filter

    actions = []

    if viable_types is None or "SEARCH_PREVIOUS" in viable_types:
        actions.append(ActionSpec(
            name="rfq_pipeline_previous_suppliers",
            payload={**base_payload},
            label="📊 Previous Sales",
            tooltip="Search purchase history for suppliers of these parts",
        ))

    if viable_types is None or "SEARCH_BRAND" in viable_types:
        actions.append(ActionSpec(
            name="rfq_pipeline_brand_suppliers",
            payload={**base_payload},
            label="🏷️ Brand Suppliers",
            tooltip="Find suppliers linked to these brands in our database",
        ))

    if viable_types is None or "SEARCH_WEB_AU" in viable_types:
        actions.append(ActionSpec(
            name="rfq_pipeline_new_domestic",
            payload={**base_payload},
            label="🇦🇺 New Australian Suppliers",
            tooltip="Search the web for Australian suppliers",
        ))

    if viable_types is None or "SEARCH_WEB_INTL" in viable_types:
        actions.append(ActionSpec(
            name="rfq_pipeline_new_international",
            payload={**base_payload},
            label="🌐 New International Suppliers",
            tooltip="Search the web globally for suppliers",
        ))

    actions.append(ActionSpec(
        name="rfq_pipeline_supplier_search_done",
        payload={**base_payload},
        label="✅ Done — Continue",
        tooltip="Finish supplier search and continue",
    ))

    return actions


async def show_search_menu(
    rfq_id: str,
    user_id: str,
    summary: str = "",
    line_filter: list[int] | None = None,
    ctx: ChatContext | None = None,
) -> None:
    """Display the supplier search menu with action buttons.

    Automatically filters options based on item data (e.g., no "Previous Sales"
    button if items lack part numbers).

    Args:
        rfq_id: The RFQ identifier.
        user_id: The current user.
        summary: Optional summary to prepend (e.g. "✅ Previous Sales complete.").
        line_filter: If set, scope the menu to specific line numbers.
        ctx: Where to send the menu. Defaults to the bound ChatContext.
    """
    from includes.tools.supplier_search_tools import get_viable_search_types

    ctx = ctx or get_chat_context()

    # Determine which search types make sense for these items
    viable = get_viable_search_types(rfq_id, line_numbers=line_filter)
    viable_types = viable["viable"]

    scope = ""
    if line_filter and len(line_filter) == 1:
        scope = f" for **line {line_filter[0]}**"
    elif line_filter:
        lines_str = ", ".join(str(l) for l in line_filter)
        scope = f" for **lines {lines_str}**"

    # Build intro text with only viable options
    option_lines = []
    if "SEARCH_PREVIOUS" in viable_types:
        option_lines.append("1. 📊 **Previous Sales** — suppliers who've sold these parts before")
    if "SEARCH_BRAND" in viable_types:
        option_lines.append(f"{len(option_lines)+1}. 🏷️ **Brand Suppliers** — suppliers linked to these brands")
    if "SEARCH_WEB_AU" in viable_types:
        option_lines.append(f"{len(option_lines)+1}. 🇦🇺 **New Australian Suppliers** — web search for domestic suppliers")
    if "SEARCH_WEB_INTL" in viable_types:
        option_lines.append(f"{len(option_lines)+1}. 🌐 **New International Suppliers** — web search globally")

    options_text = "\n".join(option_lines)
    intro = (
        f"**Supplier Search Options{scope}:**\n\n"
        f"{options_text}\n\n"
        f"Pick an option or tell me what you'd like to do."
    )

    if summary:
        message = f"{summary}\n\n{intro}"
    else:
        message = intro

    actions = build_menu_actions(rfq_id, user_id, line_filter=line_filter, viable_types=viable_types)
    await ctx.say(message, author="EagleAgent", actions=actions)
