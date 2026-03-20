import pytest
from unittest.mock import MagicMock, Mock, patch
from langchain_google_genai import ChatGoogleGenerativeAI
from includes.agents.procurement_agent import ProcurementAgent
from includes.tools.product_tools import search_products, search_brands, search_suppliers

class TestProcurementAgentInit:
    def setup_method(self):
        self.mock_model = Mock(spec=ChatGoogleGenerativeAI)
        self.agent = ProcurementAgent(model=self.mock_model, store=None)

    def test_name(self):
        assert self.agent.name == "ProcurementAgent"
        
class TestGetTools:
    def setup_method(self):
        self.mock_model = Mock(spec=ChatGoogleGenerativeAI)
        self.agent = ProcurementAgent(model=self.mock_model, store=None)

    @pytest.mark.asyncio
    async def test_returns_search_products_tool(self):
        # Ensure that it correctly wires all procurement tools
        tools = await self.agent.get_tools_async(user_id="test_user") if hasattr(self.agent, "get_tools_async") else self.agent.get_tools(user_id="test_user")
        assert len(tools) == 3
        tool_names = {t.name for t in tools}
        assert tool_names == {"search_products", "search_brands", "search_suppliers"}

class TestSystemPrompt:
    def setup_method(self):
        self.mock_model = Mock(spec=ChatGoogleGenerativeAI)
        self.agent = ProcurementAgent(model=self.mock_model, store=None)

    @pytest.mark.asyncio
    async def test_sync_prompt_has_formatting_instructions(self):
        prompt = self.agent.get_system_prompt()
        
        # Verify specific formatting instructions are injected since the user explicitly requested them
        assert "markdown table" in prompt.lower()
        assert "numbered" in prompt.lower()
        
    @pytest.mark.asyncio
    async def test_async_prompt_matches_sync(self):
        sync_prompt = self.agent.get_system_prompt()
        async_prompt = await self.agent.get_system_prompt_async(user_id="test_user")
        assert sync_prompt == async_prompt

class TestAgentCall:
    def setup_method(self):
        from unittest.mock import AsyncMock
        from langchain_core.messages import AIMessage
        self.mock_model = Mock(spec=ChatGoogleGenerativeAI)
        self.mock_model.bind_tools = Mock(return_value=self.mock_model)
        self.mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="Here are the products"))
        self.agent = ProcurementAgent(model=self.mock_model, store=None)

    @pytest.mark.asyncio
    async def test_call_creates_and_invokes_react_graph(self):
        from langchain_core.messages import HumanMessage, AIMessage
        
        state = {
            "messages": [HumanMessage(content="I need a widget")],
            "user_id": "test_user"
        }
        
        result = await self.agent(state)
        
        assert "messages" in result
        
        # Last message should be the AI response
        ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert len(ai_msgs) >= 1
        assert ai_msgs[-1].content == "Here are the products"

    @pytest.mark.asyncio
    async def test_cleanup_does_not_raise(self):
        if hasattr(self.agent, "cleanup"):
            await self.agent.cleanup()
