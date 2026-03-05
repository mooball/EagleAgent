"""
Tests for browser agent and browser tools.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage

from includes.agents.browser_agent import BrowserAgent
from includes.tools.browser_tools import browser


class TestBrowserTools:
    """Test browser automation tools."""
    
    def test_browser_tool_exists(self):
        """Test that browser tool is properly defined."""
        assert browser is not None
        assert browser.name == "browser"
        assert "browser automation" in browser.description.lower()
    
    @pytest.mark.asyncio
    @patch('asyncio.create_subprocess_exec')
    async def test_browser_open_command(self, mock_create):
        """Test browser open command execution."""
        # Mock successful command execution
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"Navigated to https://example.com\n", b"")
        mock_process.returncode = 0
        mock_create.return_value = mock_process
        
        result = await browser.ainvoke("open https://example.com")
        
        # Verify subprocess was called correctly
        mock_create.assert_called_once()
        call_args = mock_create.call_args[0]
        assert "agent-browser" in call_args
        assert "open" in call_args
        assert "https://example.com" in call_args
        
        # Verify result
        assert "example.com" in result.lower() or "navigated" in result.lower()
    
    @pytest.mark.asyncio
    @patch('asyncio.create_subprocess_exec')
    async def test_browser_snapshot_command(self, mock_create):
        """Test browser snapshot --json command."""
        # Mock snapshot JSON output
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b'{"url": "https://example.com", "elements": [{"ref": "@e1", "tag": "a"}]}', b"")
        mock_process.returncode = 0
        mock_create.return_value = mock_process
        
        result = await browser.ainvoke("snapshot --json")
        
        # Verify the command
        mock_create.assert_called_once()
        call_args = mock_create.call_args[0]
        assert "agent-browser" in call_args
        assert "snapshot" in call_args
        assert "--json" in call_args
        
        # Verify JSON output returned
        assert "@e1" in result
    
    @pytest.mark.asyncio
    @patch('asyncio.create_subprocess_exec')
    async def test_browser_error_handling(self, mock_create):
        """Test browser tool error handling."""
        # Mock failed command
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"", b"Element not found")
        mock_process.returncode = 1
        mock_create.return_value = mock_process
        
        result = await browser.ainvoke("click @e999")
        
        # Verify error is returned
        assert "error" in result.lower()
        assert "element not found" in result.lower()
    
    @pytest.mark.asyncio
    @patch('asyncio.create_subprocess_exec')
    async def test_browser_timeout_handling(self, mock_create):
        """Test browser tool timeout handling."""
        import asyncio
        
        # Mock timeout exception
        mock_process = AsyncMock()
        mock_process.communicate.side_effect = asyncio.TimeoutError()
        mock_create.return_value = mock_process
        
        result = await browser.ainvoke("open https://very-slow-site.com")
        
        # Verify timeout error is returned
        assert "error" in result.lower()
        assert "timed out" in result.lower()


@pytest.mark.asyncio
class TestBrowserAgent:
    """Test BrowserAgent class."""
    
    def setup_method(self):
        """Setup test fixtures."""
        # Create a mock model
        self.mock_model = Mock(spec=ChatGoogleGenerativeAI)
        self.mock_model.bind_tools = Mock(return_value=self.mock_model)
        
        # Create browser agent
        self.agent = BrowserAgent(model=self.mock_model, store=None)
    
    def test_agent_initialization(self):
        """Test browser agent initializes correctly."""
        assert self.agent.name == "BrowserAgent"
        assert self.agent.model is not None
    
    def test_agent_get_tools(self):
        """Test browser agent provides correct tools."""
        tools = self.agent.get_tools(user_id="test@example.com")
        
        assert len(tools) == 1
        assert tools[0].name == "browser"
    
    def test_agent_system_prompt(self):
        """Test browser agent system prompt."""
        prompt = self.agent.get_system_prompt()
        
        assert "BrowserAgent" in prompt
        assert "browser automation" in prompt.lower() or "web browsing" in prompt.lower()
        assert "snapshot" in prompt.lower()
        assert "open" in prompt.lower()
    
    @pytest.mark.asyncio
    async def test_agent_call(self):
        """Test browser agent execution."""
        # Mock the model response
        mock_response = AIMessage(content="I'll help you browse the web")
        self.mock_model.ainvoke = AsyncMock(return_value=mock_response)
        
        # Create state
        state = {
            "messages": [HumanMessage(content="Search for Python")],
            "user_id": "test@example.com"
        }
        
        # Call the agent
        result = await self.agent(state)
        
        # Verify response
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert result["messages"][0] == mock_response
        
        # Verify model was called
        self.mock_model.ainvoke.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_agent_cleanup(self):
        """Test browser agent cleanup."""
        # Should not raise any errors
        await self.agent.cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
class TestBrowserAgentIntegration:
    """Integration tests for browser agent (requires agent-browser installed)."""
    
    def setup_method(self):
        """Setup for integration tests."""
        # Import here to avoid issues if agent-browser not installed
        import os
        from dotenv import load_dotenv
        
        load_dotenv()
        
        # Only run if we have an API key
        if not os.getenv("GOOGLE_API_KEY"):
            pytest.skip("GOOGLE_API_KEY not set")
        
        # Create real model
        from config import config
        self.model = ChatGoogleGenerativeAI(
            model=config.DEFAULT_MODEL,
            google_api_key=os.getenv("GOOGLE_API_KEY")
        )
        
        self.agent = BrowserAgent(model=self.model, store=None)
    
    @pytest.mark.slow
    @pytest.mark.asyncio  
    async def test_real_browser_task(self):
        """Test browser agent with real browser task."""
        state = {
            "messages": [HumanMessage(content="Open example.com and tell me the title")],
            "user_id": "test@example.com"
        }
        
        result = await self.agent(state)
        
        # Should have a response
        assert "messages" in result
        assert len(result["messages"]) > 0
        
        # Response should mention the page
        response_text = result["messages"][-1].content
        if isinstance(response_text, list):
            # Handle structured response from Gemini
            text_parts = []
            for part in response_text:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    # Prefer explicit text field if present
                    text_value = part.get("text")
                    if isinstance(text_value, str):
                        text_parts.append(text_value)
            response_text = "".join(text_parts)
        
        assert isinstance(response_text, str)
        assert len(response_text) > 0
