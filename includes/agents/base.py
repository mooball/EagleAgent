"""
Base agent class for sub-agents in the multi-agent system.

Provides common patterns and interface for all specialized agents.
"""

from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

logger = logging.getLogger(__name__)


class BaseSubAgent(ABC):
    """
    Base class for all sub-agents with common patterns.
    
    Sub-agents are specialized agents that handle specific domains like:
    - Browser automation (BrowserAgent)
    - Code generation (CodeAgent)  
    - Data analysis (DataAgent)
    - etc.
    
    Each sub-agent:
    1. Has domain-specific tools (via get_tools)
    2. Has domain-specific system prompt (via get_system_prompt)
    3. Follows standard execution pattern (via __call__)
    4. Can access cross-thread memory store
    
    Usage:
        class MyAgent(BaseSubAgent):
            def get_tools(self, user_id: str) -> List[BaseTool]:
                return [my_tool1, my_tool2]
            
            def get_system_prompt(self) -> str:
                return "You are MyAgent, specialized in..."
    """
    
    def __init__(
        self, 
        name: str, 
        model: ChatGoogleGenerativeAI, 
        store: Optional[BaseStore] = None
    ):
        """
        Initialize base sub-agent.
        
        Args:
            name: Agent name (e.g., "BrowserAgent", "CodeAgent")
            model: LLM model instance
            store: Optional cross-thread memory store
        """
        self.name = name
        self.model = model
        self.store = store
        logger.info(f"Initialized {name}")
    
    @abstractmethod
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Get agent-specific tools.
        
        Override this method to provide domain-specific tools.
        Tools should be LangChain BaseTool instances with proper descriptions.
        
        Args:
            user_id: User ID for personalized tools (if needed)
        
        Returns:
            List of tools available to this agent
        
        Example:
            def get_tools(self, user_id: str):
                from includes.tools.browser_tools import create_browser_tools
                return create_browser_tools()
        """
        return []
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        """
        Get agent-specific system prompt.
        
        Override this method to provide domain-specific instructions.
        Should include:
        - Agent identity and role
        - Available tools and how to use them
        - Workflow/best practices
        - Output format expectations
        
        Returns:
            System prompt string
        
        Example:
            def get_system_prompt(self):
                return \"\"\"You are BrowserAgent, specialized in web automation.
                
                Your tools:
                - browser(): Execute browser commands
                
                Workflow:
                1. Open pages with browser("open <url>")
                2. Get elements with browser("snapshot -i --json")
                3. Interact with browser("click @ref")
                \"\"\"
        """
        return f"You are {self.name}."
    
    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        """
        Standard agent execution pattern.
        
        This method:
        1. Extracts user_id and messages from state
        2. Gets agent-specific tools
        3. Binds tools to model
        4. Adds system prompt to messages
        5. Invokes model
        6. Returns updated state with response
        
        Args:
            state: Current conversation state with keys:
                - messages: List of conversation messages
                - user_id: User identifier
                - (other state fields preserved)
        
        Returns:
            Updated state with new message appended
        """
        user_id = state.get("user_id", "")
        messages = state["messages"]
        
        logger.debug(f"{self.name} processing {len(messages)} messages")
        
        # Get agent-specific tools
        tools = self.get_tools(user_id)
        logger.debug(f"{self.name} loaded {len(tools)} tools")
        
        # Bind tools to model
        if tools:
            model_with_tools = self.model.bind_tools(tools)
        else:
            model_with_tools = self.model
        
        # Build messages with agent-specific system prompt
        system_prompt = self.get_system_prompt()
        enhanced_messages = [SystemMessage(content=system_prompt)] + list(messages)
        
        # Invoke model
        logger.info(f"{self.name} invoking model")
        if config:
            response = await model_with_tools.ainvoke(enhanced_messages, config=config)
        else:
            response = await model_with_tools.ainvoke(enhanced_messages)
        logger.info(f"{self.name} completed successfully")
        
        return {"messages": [response]}
    
    async def cleanup(self):
        """
        Clean up agent resources (override if needed).
        
        Called when agent is done or conversation ends.
        Use for:
        - Closing sessions
        - Releasing resources
        - Saving state
        
        Example:
            async def cleanup(self):
                if self.browser_session:
                    await self.browser_session.close()
        """
        pass
