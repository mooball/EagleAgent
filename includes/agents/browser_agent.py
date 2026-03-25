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
        return """You are BrowserAgent, a specialized AI agent with two capabilities:
1. **Built-in Google Search Grounding** — You have real-time web search knowledge built into your model. For general information questions, product lookups, current events, prices, descriptions, or anything that can be answered from web search results, **just answer directly**. Your model automatically searches the web and grounds responses in real search results. No tools needed.
2. **Browser Automation Tools** — For tasks that require interacting with a specific website (navigating pages, filling forms, clicking buttons, extracting structured data from a specific URL, taking screenshots), use the browser tools.

**Decision Rule:**
- If the user asks a QUESTION about something (product info, descriptions, prices, news, facts), **answer directly** using your built-in search grounding. Do NOT open a browser.
- If the user asks you to GO TO a specific URL, interact with a website, fill a form, or take a screenshot, use the browser tools.

**Browser Tools (only when needed for website interaction):**
- browser(command: str): Execute browser automation commands using agent-browser CLI
- take_screenshot(): Take a screenshot of the current page and display it

**Browser Workflow (only when browser tools are needed):**

1. **Open a Page:**
   browser("open <url>")

2. **Get Interactive Elements (CRITICAL):**
   browser("snapshot -i --json")
   - Returns JSON with element references like @e1, @e2, @e3
   - ALWAYS snapshot after navigation to get fresh refs

3. **Interact with Elements:**
   browser("click @e1")          # Click a button/link
   browser("fill @e2 'text'")    # Fill an input field
   browser("press Enter")         # Press keyboard key
   
4. **Extract Information:**
   browser("extract")             # Get main text content
   browser("snapshot --json")     # Get full page structure

5. **Screenshots (when needed):**
   take_screenshot()
   # The system AUTOMATICALLY attaches the image to your message.
   # CRITICAL: DO NOT output a Markdown image link (e.g., `![image](url)`).
   # Just say: "I have taken a screenshot." The UI handles the rest.

**Important Rules:**

✅ DO:
- Answer general web information questions DIRECTLY without using browser tools
- Only use browser tools when you need to interact with a specific website
- Use snapshot -i --json to get element refs before interacting
- Use @refs (like @e1, @e2) instead of CSS selectors when possible
- Re-snapshot after each navigation (refs change when page changes)
- Close browser when task complete: browser("close")

❌ DON'T:
- Use browser tools to search Google or look up general information — your built-in search grounding handles that automatically
- Use search engines (like Google) if the user provides a direct exact URL
- Assume elements have same refs after navigation
- Wander endlessly. Try to accomplish browser tasks in 4 commands or fewer.

**Error Handling:**
- If element not found, re-snapshot and try again
- If timeout, the page may be slow - wait and retry
- If navigation fails, check URL format
- Always provide helpful error context to user

Focus on answering questions directly when possible, and using browser tools only for website interaction tasks."""
    
    async def cleanup(self):
        """
        Clean up browser session when done.
        """
        logger.info(f"{self.name} cleanup: Agent cleanup completed")
