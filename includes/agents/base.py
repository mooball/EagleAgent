"""
Base agent class for sub-agents in the multi-agent system.

Provides common patterns and interface for all specialized agents.
"""

from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.messages import SystemMessage, AIMessage, trim_messages, RemoveMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Default message trim limit for all agents
DEFAULT_MAX_MESSAGES = 30

# Retry settings for transient API errors (429, 503)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds


def _is_transient_error(exc: Exception) -> bool:
    """Check if an exception is a transient API error worth retrying."""
    err_str = str(exc)
    return any(code in err_str for code in ("429", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded"))


async def _notify_retry(agent_name: str, attempt: int, max_retries: int, delay: int) -> None:
    """Send a Chainlit status message to the user on transient API retries."""
    try:
        import chainlit as cl
        await cl.Message(
            content=f"\u23f3 LLM temporarily unavailable \u2014 retrying ({attempt}/{max_retries})...",
            author="System",
        ).send()
    except Exception:
        pass  # Don't let notification failures break the retry loop


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
    
    def get_native_tools(self) -> list:
        """
        Get Gemini-native tools (e.g. Google Search grounding).
        
        These are google.genai.types.Tool objects that are passed directly to the
        Gemini API alongside LangChain tools. They are executed server-side by the
        API, not by the LangChain tool execution loop.
        
        Override this method to provide native Gemini tools for this agent.
        
        Returns:
            List of google.genai.types.Tool objects (default: empty list)
        """
        return []
    
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
        
        # Ensure all agents know the current date/time
        import datetime
        current_time = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=10))
        ).strftime("%A, %Y-%m-%d %H:%M:%S")
        if current_time not in system_prompt:
            system_prompt = (
                f"The current date and time in AEST (UTC+10) is: {current_time}.\n\n"
                + system_prompt
            )
        
        # Append procurement intent context if present in state
        intent_context = state.get("intent_context")
        if intent_context:
            system_prompt += f"\n\n**Current user intent:** {intent_context}"
        
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
        
        # Check for Gemini-native tools (e.g. Google Search grounding)
        native_tools = self.get_native_tools()
        
        if tools or native_tools:
            from langgraph.prebuilt import create_react_agent
            
            # Gemini 2.5 models cannot combine built-in tools (google_search)
            # with function calling in the same request.  When both are present,
            # use only native tools (google_search is the core capability).
            model_name = getattr(self.model, "model", "")
            if native_tools and tools and "gemini-2.5" in model_name:
                logger.info(
                    f"{self.name}: dropping {len(tools)} LangChain tool(s) for "
                    f"{model_name} — built-in tools + function calling cannot be "
                    "combined; using native tools only"
                )
                tools = []

            if native_tools:
                # When mixing native Gemini tools with LangChain tools, we must:
                # 1. Pre-bind tools + tool_config ourselves (create_react_agent's
                #    validation doesn't handle Google-format tools properly)
                # 2. Use a dynamic model function so create_react_agent skips
                #    its own bind_tools call
                native_tool_dicts = [t.model_dump() for t in native_tools]
                
                # include_server_side_tool_invocations is not supported on
                # Vertex AI, nor on Gemini 2.5/2.0 models via AI Studio.
                model_name = getattr(self.model, "model", "")
                use_vertexai = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
                bind_kwargs = {}
                if not use_vertexai and "gemini-2.5" not in model_name and "gemini-2.0" not in model_name:
                    bind_kwargs["tool_config"] = {"include_server_side_tool_invocations": True}
                
                bound_model = self.model.bind_tools(
                    list(tools) + native_tool_dicts,
                    **bind_kwargs
                )
                # Dynamic model function bypasses _should_bind_tools validation
                sub_agent_graph = create_react_agent(
                    lambda state, config: bound_model, tools
                )
            else:
                sub_agent_graph = create_react_agent(self.model, tools)
            invoke_config = {"recursion_limit": 25}
            if config is not None:
                invoke_config.update(config)
            
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    result = await sub_agent_graph.ainvoke({"messages": trimmed_messages}, config=invoke_config)
                    break
                except Exception as e:
                    if _is_transient_error(e) and attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * attempt
                        logger.warning(f"{self.name} transient API error (attempt {attempt}/{MAX_RETRIES}), retrying in {delay}s: {e}")
                        await _notify_retry(self.name, attempt, MAX_RETRIES, delay)
                        await asyncio.sleep(delay)
                    else:
                        raise
            
            response = result["messages"][-1]
        else:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if config is not None:
                        response = await self.model.ainvoke(trimmed_messages, config=config)
                    else:
                        response = await self.model.ainvoke(trimmed_messages)
                    break
                except Exception as e:
                    if _is_transient_error(e) and attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * attempt
                        logger.warning(f"{self.name} transient API error (attempt {attempt}/{MAX_RETRIES}), retrying in {delay}s: {e}")
                        await _notify_retry(self.name, attempt, MAX_RETRIES, delay)
                        await asyncio.sleep(delay)
                    else:
                        raise
                
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
