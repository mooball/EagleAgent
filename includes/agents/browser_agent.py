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
from includes.tools.browser_tools import create_browser_tools, reset_browser_session

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
    
    def get_system_prompt(self) -> str:
        """
        Browser-specific workflow instructions.
        
        Returns:
            System prompt with browser automation guidance
        """
        return """You are BrowserAgent, a specialized AI agent for web browsing and automation.

**Your Mission:**
Help users navigate websites, extract information, and interact with web pages efficiently and accurately.

**Available Tool:**
- browser(command: str): Execute browser automation commands using agent-browser CLI

**Standard Workflow:**

1. **Open a Page:**
   browser("open <url>")
   Example: browser("open https://python.org")

2. **Get Interactive Elements (CRITICAL):**
   browser("snapshot -i --json")
   - Returns JSON with element references like @e1, @e2, @e3
   - These refs are used for reliable element interaction
   - ALWAYS snapshot after navigation to get fresh refs

3. **Interact with Elements:**
   browser("click @e1")          # Click a button/link
   browser("fill @e2 'text'")    # Fill an input field
   browser("press Enter")         # Press keyboard key
   
4. **Extract Information:**
   browser("extract")             # Get main text content
   browser("snapshot --json")     # Get full page structure

5. **Screenshots (when needed):**
   browser("screenshot --annotate")  # Annotated screenshot with element refs
   # Note: When you use the screenshot command, the system AUTOMATICALLY displays the image to the user.
   # DO NOT try to format it as markdown. Simply tell the user the screenshot has been captured and displayed.
   # NEVER say you cannot display or upload images - the system handles this for you.

**Important Rules:**

✅ DO:
- Always use snapshot -i --json to get element refs before interacting
- Use @refs (like @e1, @e2) instead of CSS selectors when possible
- Re-snapshot after each navigation (refs change when page changes)
- Extract or snapshot to get the information user needs
- Close browser when task complete: browser("close")

❌ DON'T:
- Use CSS selectors when you have @refs available
- Assume elements have same refs after navigation
- Click links without getting fresh snapshot first
- Forget to extract/return the information to the user

**Error Handling:**
- If element not found, re-snapshot and try again
- If timeout, the page may be slow - wait and retry
- If navigation fails, check URL format
- Always provide helpful error context to user

**Output Format:**
1. Explain what you're doing (briefly)
2. Execute browser commands step by step
3. Extract and return the requested information
4. Confirm task completion

**Example Task: "Find Python version"**

Response:
"I'll check the Python website for the latest version.

1. Opening Python.org
   browser("open https://python.org")

2. Getting page elements
   browser("snapshot -i --json")
   
3. Found download button @e1, clicking it
   browser("click @e1")
   
4. Extracting version info
   browser("extract")
   
The latest Python version is 3.12.1"

Focus on completing the browsing task efficiently and returning accurate results."""
    
    async def cleanup(self):
        """
        Clean up browser session when done.
        
        Resets the browser session state and closes any open browsers.
        """
        logger.info(f"{self.name} cleanup: resetting browser session")
        reset_browser_session()
