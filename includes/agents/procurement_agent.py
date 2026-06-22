"""
Procurement Agent for product and supplier searches.
"""

from typing import List, Dict, Any, Optional
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from .base import BaseSubAgent
from includes.tools.product_tools import search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history
from includes.tools.quote_tools import create_quote_tools
from includes.prompts import load_prompt

logger = logging.getLogger(__name__)

# Intent keywords that indicate the user explicitly requested RFQ work
_RFQ_INTENT_KEYWORDS = ("new_rfq", "RFQ", "Request for Quote", "manage_rfq")


class ProcurementAgent(BaseSubAgent):
    """
    Specialized agent for searching products, parts, and suppliers.
    """
    
    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None, internal_only: bool = False):
        super().__init__("ProcurementAgent", model, store)
        self._rfq_active = False
        self._internal_only = internal_only
    
    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        # Determine if RFQ workflow should be active based on the thread's intent
        # or the user's current dashboard view (viewing an RFQ detail page)
        intent_context = state.get("intent_context") or ""
        self._rfq_active = any(kw in intent_context for kw in _RFQ_INTENT_KEYWORDS)
        if not self._rfq_active:
            # Check if the user is viewing an RFQ in the dashboard context
            messages = state.get("messages", [])
            if messages:
                last_msg = messages[-1]
                content = last_msg.content if hasattr(last_msg, "content") else ""
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                if "rfq_detail" in content or "RFQ-" in content:
                    self._rfq_active = True
        return await super().__call__(state, config)

    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Provide procurement tools including RFQ tools (always available
        so users can view/update existing RFQs).
        """
        tools = [search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history]
        if not self._internal_only:
            tools.extend(create_quote_tools(user_id))
        return tools
    
    def get_system_prompt(self) -> str:
        """
        Procurement-specific workflow instructions.
        """
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
            return base_prompt + "\n\n" + load_prompt("rfq_workflow")
        else:
            return base_prompt + """\n**RFQ Policy:**
You have access to RFQ tools (`get_rfq`, `manage_rfq`) and should use them when the user asks to view, update, or manage an existing RFQ.

**Internet Search Policy:**
- NEVER search the internet for suppliers or products unless explicitly asked by the user.
- Always exhaust the internal database and purchase history first.
- After presenting local results, ask the user before searching externally: "Would you like me to search the internet for additional suppliers?"

**However, do NOT create new RFQs** in this session. If the user wants to create a new RFQ, tell them to use the **RFQ section on the dashboard**. Never proactively create an RFQ just because the user mentioned a list of products or parts — only do product/supplier lookups unless an RFQ creation is explicitly requested."""
