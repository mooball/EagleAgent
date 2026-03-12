"""
Base agent class for sub-agents in the multi-agent system.

Provides common patterns and interface for all specialized agents.
"""

from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.messages import SystemMessage, trim_messages, RemoveMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

logger = logging.getLogger(__name__)

# Default message trim limit for all agents
DEFAULT_MAX_MESSAGES = 30


class BaseSubAgent(ABC):
    """
    Base class for all sub-agents with common patterns.
    
    Sub-agents are specialized agents that handle specific domains like:
    - Browser automation (BrowserAgent)
    - Code generation (CodeAgent)  
    - Data analysis (DataAgent)
    - etc.
    
    Each sub-agent:
    1. Has domain-specific tools (via get_tools or get_tools_async)
    2. Has domain-specific system prompt (via get_system_prompt or get_system_prompt_async)
    3. Follows standard execution pattern (via __call__)
    4. Can access cross-thread memory store
    5. Gets automatic message trimming and checkpoint cleanup
    
    Usage (sync tools):
        class MyAgent(BaseSubAgent):
            def get_tools(self, user_id: str) -> List[BaseTool]:
                return [my_tool1, my_tool2]
            
            def get_system_prompt(self) -> str:
                return "You are MyAgent, specialized in..."
    
    Usage (async tools, e.g. MCP):
        class MyAgent(BaseSubAgent):
            async def get_tools_async(self, user_id: str) -> List[BaseTool]:
                tools = await self.mcp_client.get_tools()
                return tools
            
            async def get_system_prompt_async(self, user_id: str) -> str:
                profile = await self.store.aget(("users",), user_id)
                return build_prompt(profile)
    """
    
    def __init__(
        self, 
        name: str, 
        model: ChatGoogleGenerativeAI, 
        store: Optional[BaseStore] = None,
        max_messages: int = DEFAULT_MAX_MESSAGES
    ):
        """
        Initialize base sub-agent.
        
        Args:
            name: Agent name (e.g., "BrowserAgent", "CodeAgent")
            model: LLM model instance
            store: Optional cross-thread memory store
            max_messages: Max messages to retain after trimming (default: 30)
        """
        self.name = name
        self.model = model
        self.store = store
        self.max_messages = max_messages
        logger.info(f"Initialized {name}")
    
    @abstractmethod
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Get agent-specific tools (sync version).
        
        Override this method to provide domain-specific tools.
        For async tool retrieval (e.g. MCP), override get_tools_async instead.
        
        Args:
            user_id: User ID for personalized tools (if needed)
        
        Returns:
            List of tools available to this agent
        """
        return []
    
    async def get_tools_async(self, user_id: str) -> List[BaseTool]:
        """
        Get agent-specific tools (async version).
        
        Override this method when tools require async retrieval (e.g. MCP client).
        By default, delegates to the sync get_tools().
        
        Args:
            user_id: User ID for personalized tools (if needed)
        
        Returns:
            List of tools available to this agent
        """
        return self.get_tools(user_id)
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        """
        Get agent-specific system prompt (sync version).
        
        Override this method to provide domain-specific instructions.
        For dynamic prompts that need async data, override get_system_prompt_async instead.
        
        Returns:
            System prompt string
        """
        return f"You are {self.name}."
    
    async def get_system_prompt_async(self, user_id: str) -> str:
        """
        Get agent-specific system prompt (async version).
        
        Override this method when building the prompt requires async operations
        (e.g. loading user profile from store).
        By default, delegates to the sync get_system_prompt().
        
        Args:
            user_id: User ID for dynamic prompt personalization
        
        Returns:
            System prompt string
        """
        return self.get_system_prompt()
    
    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        """
        Standard agent execution pattern with message trimming and checkpoint cleanup.
        
        This method:
        1. Extracts user_id and messages from state
        2. Gets agent-specific tools (async)
        3. Builds system prompt (async)
        4. Trims message history to max_messages
        5. Invokes model (with or without tools)
        6. Returns updated state with response + RemoveMessages for pruned history
        
        Args:
            state: Current conversation state with keys:
                - messages: List of conversation messages
                - user_id: User identifier
        
        Returns:
            Updated state with new message appended and pruned messages removed
        """
        user_id = state.get("user_id", "")
        messages = state["messages"]
        
        logger.debug(f"{self.name} processing {len(messages)} messages")
        
        # Get agent-specific tools (async to support MCP etc.)
        tools = await self.get_tools_async(user_id)
        logger.debug(f"{self.name} loaded {len(tools)} tools")
        
        # Build system prompt (async to support profile-based prompts)
        system_prompt = await self.get_system_prompt_async(user_id)
        
        # Prepend system prompt if not already present
        enhanced_messages = list(messages)
        if not any(isinstance(m, SystemMessage) for m in enhanced_messages):
            enhanced_messages = [SystemMessage(content=system_prompt)] + enhanced_messages
        
        # Trim message history to prevent unbounded checkpoint growth
        trimmed_messages = trim_messages(
            enhanced_messages,
            max_tokens=self.max_messages,
            strategy="last",
            token_counter=len,
            include_system=True,
            allow_partial=False
        )
        
        # Invoke model
        logger.info(f"{self.name} invoking model")
        if tools:
            from langgraph.prebuilt import create_react_agent
            sub_agent_graph = create_react_agent(self.model, tools)
            if config is not None:
                result = await sub_agent_graph.ainvoke({"messages": trimmed_messages}, config=config)
            else:
                result = await sub_agent_graph.ainvoke({"messages": trimmed_messages})
            
            response = result["messages"][-1]
        else:
            if config is not None:
                response = await self.model.ainvoke(trimmed_messages, config=config)
            else:
                response = await self.model.ainvoke(trimmed_messages)
                
        logger.info(f"{self.name} completed successfully")
        
        # Clean up old messages from checkpoint
        retained_ids = {m.id for m in trimmed_messages if getattr(m, "id", None)}
        messages_to_remove = [
            RemoveMessage(id=m.id)
            for m in messages
            if getattr(m, "id", None) and m.id not in retained_ids
        ]
        
        return {"messages": messages_to_remove + [response]}
    
    async def cleanup(self):
        """
        Clean up agent resources (override if needed).
        
        Called when agent is done or conversation ends.
        Use for:
        - Closing sessions
        - Releasing resources
        - Saving state
        """
        pass
