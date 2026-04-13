"""
Research agent with Google Search grounding for web research and analysis.

This agent is used via the "Research Agent" chat profile, providing users
with a focused interface for web research, information gathering, and
analysis powered by Gemini's native Google Search grounding.
"""

from typing import List, Optional
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
from google.genai import types as genai_types
import logging

from includes.agents.base import BaseSubAgent
from includes.tools.user_profile import create_profile_tools
from includes.tools.quote_tools import create_quote_tools
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
    ):
        super().__init__("ResearchAgent", model, store)

    def get_tools(self, user_id: str) -> List[BaseTool]:
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))
            tools.extend(create_quote_tools(self.store, user_id))
        return tools

    def get_native_tools(self) -> list:
        """Return Gemini-native Google Search grounding tool."""
        return [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    def get_system_prompt(self) -> str:
        return build_research_prompt({})

    async def get_system_prompt_async(self, user_id: str) -> str:
        user_profile = None
        if user_id and self.store:
            user_profile = await self.store.aget(("users",), user_id)
        profile_data = dict(user_profile.value) if (user_profile and user_profile.value) else {}
        return build_research_prompt(profile_data)
