"""
Browser automation agent for web browsing and data extraction.

Uses agent-browser CLI for headless browser automation.
"""

from typing import List
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from .base import BaseSubAgent
from includes.tools.browser_tools import create_browser_tools
from includes.prompts import load_prompt
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


class BrowserAgent(BaseSubAgent):
    """
    Specialized agent for web browsing and automation.
    
    Capabilities:
    - Navigate to web pages
    - Extract information from pages
    - Interact with forms and buttons
    - Take screenshots
    - Handle dynamic content
    
    Uses agent-browser CLI under the hood for browser automation.
    """
    
    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None):
        """
        Initialize Browser Agent.
        
        Args:
            model: LLM model instance
            store: Optional cross-thread memory store
        """
        super().__init__("BrowserAgent", model, store)
    
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Provide browser-specific tools.
        
        Args:
            user_id: User identifier (unused for browser tools)
        
        Returns:
            List containing the browser() tool
        """
        return create_browser_tools()
    
    def get_native_tools(self) -> list:
        """Return Gemini-native tools like Google Search grounding."""
        return [genai_types.Tool(google_search=genai_types.GoogleSearch())]
    
    def get_system_prompt(self) -> str:
        """
        Browser-specific workflow instructions.
        
        Returns:
            System prompt with browser automation guidance
        """
        return load_prompt("browser_agent")
    
    async def cleanup(self) -> None:
        """
        Clean up browser session when done.
        """
        logger.info(f"{self.name} cleanup: Agent cleanup completed")
