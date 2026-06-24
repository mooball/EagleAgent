"""
Research agent with Google Search grounding for web research and analysis.

This agent is used via the "Research Agent" chat profile, providing users
with a focused interface for web research, information gathering, and
analysis powered by Gemini's native Google Search grounding.
"""

from typing import Dict, Any, List, Optional
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
from langchain_core.runnables import RunnableConfig
from google.genai import types as genai_types
import logging

from includes.agents.base import BaseSubAgent
from includes.tools.user_profile import create_profile_tools
from includes.prompts import build_research_prompt

logger = logging.getLogger(__name__)


class ResearchAgent(BaseSubAgent):
    """
    Research agent with Google Search grounding.

    Uses Gemini's native Google Search tool for real-time web search,
    executed server-side by the Gemini API. Also includes user profile
    tools for personalization.
    """

    def __init__(
        self,
        model: ChatGoogleGenerativeAI,
        store: Optional[BaseStore] = None,
        include_rfq_tools: bool = False,
    ):
        super().__init__("ResearchAgent", model, store)
        self.include_rfq_tools = include_rfq_tools

    def get_tools(self, user_id: str) -> List[BaseTool]:
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))
            if self.include_rfq_tools:
                from includes.tools.quote_tools import create_quote_tools
                # Only give ResearchAgent read-only RFQ access (get_rfq)
                # It must NOT add suppliers or modify the RFQ directly
                all_rfq_tools = create_quote_tools(user_id)
                tools.extend([t for t in all_rfq_tools if t.name == "get_rfq"])
        return tools

    def get_native_tools(self) -> list:
        """Return Gemini-native Google Search grounding tool."""
        return [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    def get_system_prompt(self) -> str:
        return build_research_prompt({}, embedded=self.include_rfq_tools)

    async def get_system_prompt_async(self, user_id: str) -> str:
        user_profile = None
        if user_id and self.store:
            user_profile = await self.store.aget(("users",), user_id)
        profile_data = dict(user_profile.value) if (user_profile and user_profile.value) else {}
        return build_research_prompt(profile_data, embedded=self.include_rfq_tools)

    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        """Override to scope message context when running in multi-agent graph.

        When embedded (include_rfq_tools=True), pass the last few messages
        so the agent understands the task context (e.g. user replying "yes"
        to a supplier search question).
        """
        if not self.include_rfq_tools:
            # Standalone profile — use full conversation as normal
            return await super().__call__(state, config)

        # Embedded mode: pass the last 4 messages for context
        # (typically: dashboard context + pipeline response + user reply)
        messages = state.get("messages", [])
        if not messages:
            return await super().__call__(state, config)

        # Keep enough context for the agent to understand the task
        recent_messages = messages[-4:] if len(messages) > 4 else messages

        scoped_state = {
            **state,
            "messages": list(recent_messages),
        }
        return await super().__call__(scoped_state, config)
