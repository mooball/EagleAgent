"""
Browser automation tools using agent-browser CLI.

Provides a simple interface to the agent-browser command-line tool
for web browsing and automation tasks.
"""

import subprocess
import logging
import json
import os
import re
import chainlit as cl
from typing import List, Optional
from langchain_core.tools import tool
from config import config

logger = logging.getLogger(__name__)


@tool
async def browser(command: str) -> str:
    """
    Execute browser automation commands using agent-browser CLI.
    
    This tool provides access to a headless Chromium browser for web automation.
    The browser session persists between commands, maintaining state and cookies.
    
    **Workflow:**
    
    1. **Open a page:**
       browser("open https://example.com")
    
    2. **Get interactive elements:**
       browser("snapshot -i --json")
       Returns JSON with element refs like @e1, @e2 for buttons, links, inputs
    
    3. **Interact with elements:**
       browser("click @e1")
       browser("fill @e2 'search text'")
       browser("press Enter")
    
    4. **Extract information:**
       browser("snapshot --json")  # Full page content
       browser("extract")          # Main text content
    
    5. **Take screenshots:**
       browser("screenshot --annotate")  # Screenshot with element annotations
    
    **Common commands:**
    - open <url> - Navigate to URL
    - click <selector|@ref> - Click element
    - fill <selector|@ref> <text> - Fill input field
    - type <selector|@ref> <text> - Type into field
    - press <key> - Press keyboard key (Enter, Tab, etc.)
    - snapshot [--interactive|-i] [--json] - Get page structure
    - extract - Extract main text content
    - screenshot [--annotate|-a] - Take screenshot
    - back - Go back in history
    - forward - Go forward in history
    - refresh - Reload page
    - close - Close browser session
    
    **Element selectors:**
    - CSS selectors: "button.submit", "#login-form input[type=email]"
    - Text content: "text=Login", "text=/Sign.*/"
    - Refs from snapshot: "@e1", "@e2" (recommended - more reliable)
    
    **Important notes:**
    - Browser session persists between commands until explicitly closed
    - Always re-snapshot after navigation to get fresh element refs
    - Element refs (@e1, @e2) change after navigation
    - Use --json flag for programmatic output
    
    Args:
        command: The agent-browser command to execute (without 'agent-browser' prefix)
    
    Returns:
        Command output as string. For --json commands, returns JSON string.
        For errors, returns error message.
    
    Examples:
        >>> browser("open https://google.com")
        'Navigated to https://google.com'
        
        >>> browser("snapshot -i --json")
        '{"url": "https://google.com", "elements": [{"ref": "@e1", "tag": "input", ...}]}'
        
        >>> browser("fill @e1 'python tutorials'")
        'Filled @e1 with: python tutorials'
        
        >>> browser("press Enter")
        'Pressed key: Enter'
    """
    try:
        # Determine the temp directory and create an explicit path for screenshots if needed
        import tempfile
        import time
        
        # Rewrite screenshot command to force saving into the correct directory
        if "screenshot" in command and not ".png" in command and not "path" in command:
            # Create a dedicated directory for browser temp files inside TEMP_FILES_FOLDER
            screenshots_dir = os.path.join(config.TEMP_FILES_FOLDER, "browser_screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)
            
            # Generate a distinct filepath with absolute path to avoid CSS selector confusion in CLI
            timestamp = int(time.time() * 1000)
            custom_path = os.path.abspath(os.path.join(screenshots_dir, f"screenshot_{timestamp}.png"))
            
            # Inject the custom path into the command
            # This handles things like "screenshot --annotate" -> "screenshot --annotate .files/browser_screenshots/screenshot_123.png"
            parts = command.split()
            # Find insertion point (after 'screenshot' and any flags)
            for i, part in enumerate(parts):
                if part == "screenshot":
                    # Put it at the end of the command
                    command = f"{command} {custom_path}"
                    break
        
        # Build the command arguments using shlex to prevent shell injection
        import shlex
        args = ["agent-browser"] + shlex.split(command)
        
        logger.info(f"Executing browser command: {args}")
        
        # Execute the command asynchronously to not block the event loop
        import asyncio
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
            output_str = stdout.decode('utf-8').strip()
            error_str = stderr.decode('utf-8').strip()
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            logger.error(f"Browser command timed out: {command}")
            return "Error: Command timed out after 30 seconds"
        except Exception as e:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise e
            
        # Check for errors
        if process.returncode != 0:
            error_msg = error_str or output_str
            logger.error(f"Browser command failed: {error_msg}")
            return f"Error: {error_msg}"
        
        # Get output
        output = output_str
        
        # Strip ANSI escape codes to ensure clean regex matching and path resolving
        clean_output = re.sub(r'\x1B(?:\[[0-?]*[ -/]*[@-~])', '', output)
        
        # Special handling for screenshots: Extract path and send to Chainlit UI
        if "Screenshot saved to" in clean_output:
            # Extract the file path using regex
            match = re.search(r"Screenshot saved to\s+(.+?)(?:\n|$)", clean_output)
            if match:
                screenshot_path = match.group(1).strip()
                logger.info(f"Regex matched path: {screenshot_path}")
                if os.path.exists(screenshot_path):
                    logger.info("Screenshot path exists, proceeding to UI injection.")
                    
                    try:
                        # We send it directly to the UI! Chainlit handles the file upload
                        image_element = cl.Image(
                            path=screenshot_path,
                            name="Browser Screenshot",
                            display="inline"
                        )
                        
                        # We are now natively async, so we can await cl.Message directly!
                        await cl.Message(
                            content="📸 Here is the screenshot from the browser:",
                            elements=[image_element]
                        ).send()
                        
                        logger.info(f"Screenshot sent to UI from {screenshot_path}")
                        
                        # Return an updated output so the LLM knows it was shown to user
                        output = f"{output}\n\n[System Note: This screenshot has been directly displayed to the user in the chat UI. You should inform the user that you have successfully taken and displayed it. NEVER say you cannot display images.]"
                    except Exception as e:
                        logger.error(f"Failed to display screenshot in UI: {e}")
                        # If UI injection fails (e.g. no Chainlit context), let the LLM know where it is
                        output = f"{output}\n\n[System Note: The screenshot was saved to {screenshot_path}, but could not be automatically displayed to the UI.]"
            
        logger.info(f"Browser command succeeded: {len(output)} chars output")
        return output
        
    except subprocess.TimeoutExpired:
        logger.error(f"Browser command timed out: {command}")
        return "Error: Command timed out after 30 seconds"
    
    except Exception as e:
        logger.error(f"Browser command error: {e}")
        return f"Error: {str(e)}"


def create_browser_tools() -> List:
    """
    Create list of browser tools.
    
    Returns:
        List of browser tools (currently just one tool, but structured for future expansion)
    """
    return [browser]
