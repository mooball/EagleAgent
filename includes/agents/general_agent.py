from typing import List, Optional, Dict, Any
from langchain_core.tools import BaseTool
from langchain_core.messages import SystemMessage, trim_messages, RemoveMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
from langgraph.prebuilt import create_react_agent
import logging

from includes.agents.base import BaseSubAgent
from includes.tools.user_profile import create_profile_tools
from includes.prompts import build_system_prompt
from config import config

logger = logging.getLogger(__name__)

class GeneralAgent(BaseSubAgent):
    """
    General conversation agent that handles normal requests, uses profile tools, and MCP tools.
    """
    
    def __init__(
        self, 
        model: ChatGoogleGenerativeAI, 
        store: Optional[BaseStore] = None,
        mcp_client = None,
        admin_only_tools: List[str] = None
    ):
        super().__init__("GeneralAgent", model, store)
        self.mcp_client = mcp_client
        self.admin_only_tools = admin_only_tools or []

    async def get_tools_async(self, user_id: str) -> tuple[List[BaseTool], str]:
        """Async tools retrieval since MCP needs an await"""
        # Determine user role
        user_role = "Admin" if user_id and user_id.lower() in config.get_admin_emails() else "Staff"
        
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))
        
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
            
        return tools, user_role

    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Provide empty sync get_tools to satisfy base class. We won't use it directly."""
        return []
        
    def get_system_prompt(self) -> str:
        """Not used directly, as we use build_system_prompt dynamically in __call__"""
        return "" 

    async def __call__(self, state: Dict[str, Any], config_runnable=None) -> Dict[str, Any]:
        """Execute the general agent, managing its own system prompt and trimming."""
        user_id = state.get("user_id", "")
        messages = state["messages"]
        
        # 1. Get user role and tools asynchronously
        tools, user_role = await self.get_tools_async(user_id)
        
        # 2. Get user profile
        user_profile = None
        if user_id and self.store:
            user_profile = await self.store.aget(("users",), user_id)
            
        profile_data = dict(user_profile.value) if (user_profile and user_profile.value) else {}
        profile_data["role"] = user_role

        # 3. Build system prompt using dynamic tool names
        tool_names = [t.name for t in tools] if tools else None
        system_content = build_system_prompt(profile_data, available_tool_names=tool_names)
        
        enhanced_messages = list(messages)
        if not any(isinstance(m, SystemMessage) for m in enhanced_messages):
            enhanced_messages = [SystemMessage(content=system_content)] + enhanced_messages
            
        # 4. Trim history
        trimmed_messages = trim_messages(
            enhanced_messages,
            max_tokens=30, # Max number of messages to retain
            strategy="last",
            token_counter=len, 
            include_system=True,
            allow_partial=False
        )

        # Build sub-graph agent
        if tools:
            sub_agent_graph = create_react_agent(self.model, tools)
            if config_runnable:
                result = await sub_agent_graph.ainvoke({"messages": trimmed_messages}, config=config_runnable)
            else:
                result = await sub_agent_graph.ainvoke({"messages": trimmed_messages})
            # Messages from sub_graph append to what we sent it
            response = result["messages"][-1]
        else:
            if config_runnable:
                response = await self.model.ainvoke(trimmed_messages, config=config_runnable)
            else:
                response = await self.model.ainvoke(trimmed_messages)

        # 5. Clean up old messages from checkpoint
        retained_ids = {m.id for m in trimmed_messages if getattr(m, "id", None)}
        messages_to_remove = [
            RemoveMessage(id=m.id) 
            for m in messages 
            if getattr(m, "id", None) and m.id not in retained_ids
        ]
        
        return {"messages": messages_to_remove + [response]}
