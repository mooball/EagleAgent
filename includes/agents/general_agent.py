from typing import List, Optional, Dict, Any
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from includes.agents.base import BaseSubAgent
from includes.tools.user_profile import create_profile_tools
from includes.tools.action_tools import create_action_tools
from includes.prompts import build_system_prompt
from config import config
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

class GeneralAgent(BaseSubAgent):
    """
    General conversation agent that handles normal requests, uses profile tools, and MCP tools.
    
    Uses async hooks from BaseSubAgent for:
    - get_tools_async: MCP tool retrieval requires await
    - get_system_prompt_async: User profile lookup requires await
    """
    
    def __init__(
        self, 
        model: ChatGoogleGenerativeAI, 
        store: Optional[BaseStore] = None,
        mcp_client = None,
        admin_only_tools: List[str] = None,
    ):
        super().__init__("GeneralAgent", model, store)
        self.mcp_client = mcp_client
        self.admin_only_tools = admin_only_tools or []
        self._last_user_role = "Staff"  # Cached for prompt building

    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Sync tools - returns profile tools only (no MCP)."""
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))
        return tools

    async def get_tools_async(self, user_id: str) -> List[BaseTool]:
        """Async tools retrieval including MCP tools and role-based filtering."""
        user_role = "Admin" if user_id and user_id.lower() in config.get_admin_emails() else "Staff"
        self._last_user_role = user_role
        
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))

        # Action tools (list actions, new conversation, delete data)
        tools.extend(create_action_tools(user_id))

        # Add MCP tools if available
        if self.mcp_client:
            try:
                mcp_tools = await self.mcp_client.get_tools()
                tools.extend(mcp_tools)
                logger.debug(f"Added {len(mcp_tools)} MCP tools")
            except Exception as e:
                logger.error(f"Failed to get MCP tools: {e}")
                
        # Filter tools based on role
        if user_role != "Admin":
            tools = [t for t in tools if getattr(t, "name", "") not in self.admin_only_tools]
            
        return tools

    def get_native_tools(self) -> list:
        """Return Gemini-native tools like Google Search grounding."""
        return [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    def get_system_prompt(self) -> str:
        """Fallback sync prompt (used if get_system_prompt_async is not called)."""
        return build_system_prompt({}, available_tool_names=None)

    async def get_system_prompt_async(self, user_id: str) -> str:
        """Build dynamic system prompt from user profile and available tools."""
        user_profile = None
        if user_id and self.store:
            user_profile = await self.store.aget(("users",), user_id)
            
        profile_data = dict(user_profile.value) if (user_profile and user_profile.value) else {}
        profile_data["role"] = self._last_user_role

        # get_tools_async is called before this in base __call__, so _last_user_role is set
        # We don't re-fetch tools here; the base class handles tool binding
        return build_system_prompt(profile_data, available_tool_names=None)
