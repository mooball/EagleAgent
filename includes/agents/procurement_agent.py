"""
Procurement Agent for product and supplier searches.

Architecture: ReAct agent with supplier search tools. No pipeline state machine.
The agent uses tools when asked, suggests next steps, and a button menu provides
shortcuts. Both buttons and typed requests call the same underlying functions.
"""

import asyncio
import re
from typing import List, Dict, Any, Optional
from langchain_core.messages import AIMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from .base import BaseSubAgent
from includes.tools.product_tools import search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history, part_sale_history_batch
from includes.tools.quote_tools import create_quote_tools
from includes.tools.supplier_quote_pipeline import create_supplier_quote_tools
from includes.currency import convert_currency
from includes.prompts import load_prompt

logger = logging.getLogger(__name__)

# Intent keywords that indicate the user explicitly requested RFQ work
_RFQ_INTENT_KEYWORDS = ("new_rfq", "RFQ", "Request for Quote", "manage_rfq")


class ProcurementAgent(BaseSubAgent):
    """
    Specialized agent for searching products, parts, and suppliers.

    Architecture: ReAct agent with supplier search tools. For RUN_WORKFLOW
    intent, auto-classifies items if needed and shows a search menu, then
    falls through to ReAct so the agent can respond to typed requests or
    follow up on button clicks.
    """

    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None, internal_only: bool = False):
        super().__init__("ProcurementAgent", model, store)
        self._rfq_active = False
        self._internal_only = internal_only

    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        # Determine if RFQ workflow should be active
        intent_context = state.get("intent_context") or ""
        self._rfq_active = any(kw in intent_context for kw in _RFQ_INTENT_KEYWORDS)
        if not self._rfq_active:
            messages = state.get("messages", [])
            for msg in reversed(messages):
                content = msg.content if hasattr(msg, "content") else ""
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                if "rfq_detail" in content or "RFQ-" in content:
                    self._rfq_active = True
                    break

        logger.info(f"ProcurementAgent: _rfq_active={self._rfq_active}, internal_only={self._internal_only}")

        intent = state.get("intent", "")
        self._current_intent = intent

        # ---- RUN_WORKFLOW: auto-classify if needed, show menu, then ReAct ----
        if intent == "RUN_WORKFLOW" and not self._internal_only:
            result = await self._handle_run_workflow(state)
            if result is not None:
                return result

        # ---- Fall through to ReAct agent for all other intents ----
        # The agent has supplier search tools and will handle typed requests
        # like "find previous sales for line 10" via tool calls.
        return await super().__call__(state, config)

    async def _handle_run_workflow(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle RUN_WORKFLOW intent: classify the user's supplier search request.

        Uses an LLM classifier to determine what the user wants:
        - If they specified a direction → fall through to ReAct (agent executes)
        - If they're generic ("find suppliers") → show the button menu

        Returns a response dict if handled (menu shown), or None to fall through to ReAct.
        """
        from includes.tools.rfq_crud import _get_rfq_dict_sync
        from includes.tools.supplier_search_tools import run_classify_sync, _set_pipeline_stage_sync
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working
        from includes.chat.supplier_search_gate import show_search_menu
        from includes.intent_classifier import classify_intent

        messages = state.get("messages", [])

        # Extract RFQ ID
        rfq_id = self._extract_rfq_id_from_messages(messages)
        if not rfq_id:
            user_id = state.get("user_id", "")
            if user_id:
                from includes.dashboard.context import lookup_context
                ctx = lookup_context(user_id, state.get("thread_id"))
                if ctx and ctx.get("id", "").startswith("RFQ-"):
                    rfq_id = ctx["id"]
        if not rfq_id:
            return None  # Fall through to ReAct

        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            return None

        stage = rfq_dict.get("pipeline_stage", "unprocessed")
        user_id = state.get("user_id", "")
        items = rfq_dict.get("items", [])
        has_unmatched = any(i.get("match") == "unmatched" for i in items)

        # If items need classification, do it automatically
        if stage == "unprocessed" or has_unmatched:
            await _notify_agent_working("Classifying items...")
            await asyncio.to_thread(run_classify_sync, rfq_id, user_id)
            await _notify_rfq_updated()

        # Classify the user's supplier search intent
        user_msg = self._get_last_human_text(messages) or ""

        # Determine which search types are viable for the items in scope
        from includes.tools.supplier_search_tools import get_viable_search_types
        line_filter = None
        line_match = re.search(r'\bline\s+(\d+)', user_msg, re.IGNORECASE)
        if line_match:
            line_filter = [int(line_match.group(1))]

        viable = await asyncio.to_thread(get_viable_search_types, rfq_id, line_filter)
        viable_types = viable["viable"]

        # Build directions list with only viable search options
        all_directions = [
            {"id": "SEARCH_PREVIOUS", "description": "User wants to search purchase history / previous sales for existing suppliers"},
            {"id": "SEARCH_BRAND", "description": "User wants to find suppliers linked to item brands in our database"},
            {"id": "SEARCH_WEB_AU", "description": "User wants to search the web for Australian / domestic suppliers"},
            {"id": "SEARCH_WEB_INTL", "description": "User wants to search the web for international / global suppliers"},
        ]
        directions = [d for d in all_directions if d["id"] in viable_types]
        directions.append({"id": "SHOW_MENU", "description": "User wants to find suppliers but hasn't specified which type of search — they need to see the options"})

        context = f"Active RFQ: {rfq_id}. Pipeline stage: {stage}. The user is working on supplier search for an RFQ."
        direction = await classify_intent(user_msg, directions, context)

        # If user specified a direction → fall through to ReAct
        # The agent has the tools and prompt to execute the right search
        if direction in ("SEARCH_PREVIOUS", "SEARCH_BRAND", "SEARCH_WEB_AU", "SEARCH_WEB_INTL"):
            return None

        # User is generic / SHOW_MENU / OTHER → show the button menu
        await show_search_menu(rfq_id, user_id, line_filter=line_filter)

        scope_text = f" for line {line_filter[0]}" if line_filter else ""
        return self._make_response(
            state,
            f"How would you like to search for suppliers{scope_text}? Pick an option above."
        )
        return None

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _get_last_human_text(messages: list) -> str | None:
        """Extract the text of the last human message."""
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "human":
                content = msg.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
        return None

    def _extract_rfq_id(self, content: str) -> Optional[str]:
        """Extract RFQ ID from message content."""
        match = re.search(r"(RFQ-\d{4}-\d{4,})", content)
        return match.group(1) if match else None

    def _extract_rfq_id_from_messages(self, messages: list) -> Optional[str]:
        """Scan messages for an RFQ ID, falling back to dashboard context."""
        for msg in reversed(messages):
            content = msg.content if hasattr(msg, "content") else ""
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            rfq_id = self._extract_rfq_id(content)
            if rfq_id:
                return rfq_id
        return None

    def _make_response(self, state: Dict[str, Any], text: str) -> Dict[str, Any]:
        """Build a state update with an AIMessage response."""
        return {"messages": [AIMessage(content=text)]}

    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Provide procurement tools including supplier search tools."""
        tools = [search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history, part_sale_history_batch, convert_currency]
        if not self._internal_only:
            tools.extend(create_quote_tools(user_id))
            tools.extend(create_supplier_quote_tools(user_id))
            # Supplier search tools (classify, search previous/brand/web, menu, done)
            from includes.tools.supplier_search_tools import create_supplier_search_tools
            tools.extend(create_supplier_search_tools(user_id))
        return tools

    def get_system_prompt(self) -> str:
        """Procurement-specific workflow instructions."""
        base_prompt = load_prompt("procurement_agent")

        if self._internal_only:
            return base_prompt + """\n\n**Internal Agent Profile Boundaries:**
You are running as the Internal Agent — a database-only assistant. You do NOT have access to:
- Web browsing or internet search of any kind
- Google Search grounding
- Research tools (no product research, no supply chain research)
- RFQ management tools (no creating, viewing, or editing RFQs)
- Data deletion tools
- Server administration or script-running tools

Your ONLY capabilities are searching the internal database for products, brands, suppliers, and purchase history. If the user asks about RFQs, tell them to switch to the **Eagle Agent** profile. If they ask for web research, suggest the **Research Agent** profile."""
        elif self._rfq_active:
            if getattr(self, "_current_intent", "") == "RUN_WORKFLOW":
                prompt = base_prompt + "\n\n" + load_prompt("rfq_workflow")
            else:
                prompt = base_prompt
            return prompt
        else:
            return base_prompt + """\n**RFQ Policy:**
You have access to RFQ tools (`get_rfq`, `manage_rfq`) and should use them when the user asks to view, update, or manage an existing RFQ.

**Internet Search Policy:**
- NEVER search the internet for suppliers or products unless explicitly asked by the user.
- Always exhaust the internal database and purchase history first.
- After presenting local results, ask the user before searching externally: "Would you like me to search the internet for additional suppliers?"

**However, do NOT create new RFQs** in this session. If the user wants to create a new RFQ, tell them to use the **RFQ section on the dashboard**. Never proactively create an RFQ just because the user mentioned a list of products or parts — only do product/supplier lookups unless an RFQ creation is explicitly requested."""
