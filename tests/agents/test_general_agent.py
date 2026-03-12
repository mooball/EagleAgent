"""
Tests for GeneralAgent.

Covers:
- Initialization and configuration
- Sync and async tool retrieval (including MCP and role filtering)
- Sync and async system prompt building
- Full agent execution via __call__
- Edge cases: no store, no MCP, admin vs staff
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from includes.agents import GeneralAgent


@pytest.fixture
def mock_model():
    model = Mock(spec=ChatGoogleGenerativeAI)
    model.bind_tools = Mock(return_value=model)
    model.ainvoke = AsyncMock(return_value=AIMessage(content="Hello!"))
    return model


@pytest.fixture
def mock_store():
    store = AsyncMock(spec=BaseStore)
    # Default: return a profile with preferred_name
    profile_item = Mock()
    profile_item.value = {
        "first_name": "Tom",
        "last_name": "Smith",
        "preferred_name": "Tommy",
        "email": "tom@example.com",
        "role": "Staff",
    }
    store.aget = AsyncMock(return_value=profile_item)
    return store


@pytest.fixture
def mock_mcp_client():
    client = AsyncMock()
    mcp_tool = Mock(spec=BaseTool)
    mcp_tool.name = "web_search"
    client.get_tools = AsyncMock(return_value=[mcp_tool])
    return client


@pytest.fixture
def agent(mock_model, mock_store):
    return GeneralAgent(model=mock_model, store=mock_store)


@pytest.fixture
def agent_with_mcp(mock_model, mock_store, mock_mcp_client):
    return GeneralAgent(
        model=mock_model,
        store=mock_store,
        mcp_client=mock_mcp_client,
        admin_only_tools=["admin_tool"],
    )


# ============================================================================
# Initialization
# ============================================================================

class TestGeneralAgentInit:

    def test_name(self, agent):
        assert agent.name == "GeneralAgent"

    def test_store_assigned(self, agent, mock_store):
        assert agent.store is mock_store

    def test_default_role_cache(self, agent):
        assert agent._last_user_role == "Staff"

    def test_admin_only_tools_default_empty(self, agent):
        assert agent.admin_only_tools == []

    def test_admin_only_tools_custom(self, mock_model, mock_store):
        agent = GeneralAgent(
            model=mock_model,
            store=mock_store,
            admin_only_tools=["secret_tool"],
        )
        assert agent.admin_only_tools == ["secret_tool"]


# ============================================================================
# Sync tools (get_tools)
# ============================================================================

class TestGetToolsSync:

    def test_returns_profile_tools(self, agent):
        tools = agent.get_tools(user_id="tom@example.com")
        tool_names = [t.name for t in tools]
        assert "remember_user_info" in tool_names
        assert "get_user_info" in tool_names

    def test_no_user_id_returns_empty(self, agent):
        assert agent.get_tools(user_id="") == []

    def test_no_store_returns_empty(self, mock_model):
        agent = GeneralAgent(model=mock_model, store=None)
        assert agent.get_tools(user_id="tom@example.com") == []


# ============================================================================
# Async tools (get_tools_async) — MCP + role filtering
# ============================================================================

@pytest.mark.asyncio
class TestGetToolsAsync:

    async def test_includes_profile_tools(self, agent):
        tools = await agent.get_tools_async("tom@example.com")
        tool_names = [t.name for t in tools]
        assert "remember_user_info" in tool_names

    async def test_includes_mcp_tools(self, agent_with_mcp, mock_mcp_client):
        tools = await agent_with_mcp.get_tools_async("tom@example.com")
        tool_names = [t.name for t in tools]
        assert "web_search" in tool_names
        mock_mcp_client.get_tools.assert_awaited_once()

    async def test_mcp_failure_graceful(self, agent_with_mcp, mock_mcp_client):
        """MCP failure should not crash — just skip MCP tools."""
        mock_mcp_client.get_tools.side_effect = RuntimeError("connection lost")
        tools = await agent_with_mcp.get_tools_async("tom@example.com")
        # Should still have profile tools
        tool_names = [t.name for t in tools]
        assert "remember_user_info" in tool_names
        assert "web_search" not in tool_names

    async def test_no_mcp_client(self, agent):
        """Agent without MCP client should still return profile tools."""
        tools = await agent.get_tools_async("tom@example.com")
        assert len(tools) > 0

    @patch("includes.agents.general_agent.config")
    async def test_staff_role_filters_admin_tools(self, mock_config, agent_with_mcp):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        tools = await agent_with_mcp.get_tools_async("staff@example.com")
        tool_names = [t.name for t in tools]
        assert "admin_tool" not in tool_names
        assert agent_with_mcp._last_user_role == "Staff"

    @patch("includes.agents.general_agent.config")
    async def test_admin_role_keeps_all_tools(self, mock_config, mock_model, mock_store, mock_mcp_client):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        # Add a mock "admin_tool" to MCP tools
        admin_tool = Mock(spec=BaseTool)
        admin_tool.name = "admin_tool"
        mock_mcp_client.get_tools.return_value = [admin_tool]

        agent = GeneralAgent(
            model=mock_model,
            store=mock_store,
            mcp_client=mock_mcp_client,
            admin_only_tools=["admin_tool"],
        )
        tools = await agent.get_tools_async("admin@example.com")
        tool_names = [t.name for t in tools]
        assert "admin_tool" in tool_names
        assert agent._last_user_role == "Admin"


# ============================================================================
# System prompt
# ============================================================================

@pytest.mark.asyncio
class TestSystemPrompt:

    def test_sync_prompt_returns_string(self, agent):
        prompt = agent.get_system_prompt()
        assert isinstance(prompt, str)
        assert "EagleAgent" in prompt

    async def test_async_prompt_includes_profile(self, agent, mock_store):
        prompt = await agent.get_system_prompt_async("tom@example.com")
        assert isinstance(prompt, str)
        assert "EagleAgent" in prompt
        mock_store.aget.assert_awaited_with(("users",), "tom@example.com")

    async def test_async_prompt_no_profile(self, mock_model):
        """Agent with no store still produces a valid prompt."""
        agent = GeneralAgent(model=mock_model, store=None)
        prompt = await agent.get_system_prompt_async("tom@example.com")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    async def test_async_prompt_empty_profile(self, agent, mock_store):
        """Empty profile in store should not crash."""
        empty_item = Mock()
        empty_item.value = None
        mock_store.aget.return_value = empty_item

        prompt = await agent.get_system_prompt_async("tom@example.com")
        assert isinstance(prompt, str)


# ============================================================================
# Agent execution (__call__)
# ============================================================================

@pytest.mark.asyncio
class TestAgentCall:

    async def test_basic_call_returns_messages(self, agent, mock_model):
        state = {
            "messages": [HumanMessage(content="Hi there")],
            "user_id": "tom@example.com",
        }
        result = await agent(state)
        assert "messages" in result
        # Last message should be the AI response
        ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        assert ai_msgs[0].content == "Hello!"

    async def test_call_with_no_user_id(self, mock_model):
        """Agent should work even without a user_id."""
        agent = GeneralAgent(model=mock_model, store=None)
        state = {
            "messages": [HumanMessage(content="Hello")],
            "user_id": "",
        }
        result = await agent(state)
        assert "messages" in result

    async def test_call_trims_long_history(self, mock_model, mock_store):
        """Messages beyond max_messages should produce RemoveMessage entries."""
        agent = GeneralAgent(model=mock_model, store=mock_store)
        agent.max_messages = 5
        # Create 10 messages with IDs
        msgs = []
        for i in range(10):
            m = HumanMessage(content=f"msg {i}")
            m.id = f"msg-{i}"
            msgs.append(m)

        state = {"messages": msgs, "user_id": "tom@example.com"}
        result = await agent(state)

        from langchain_core.messages import RemoveMessage
        remove_msgs = [m for m in result["messages"] if isinstance(m, RemoveMessage)]
        assert len(remove_msgs) > 0, "Expected some messages to be pruned"

    async def test_cleanup_does_not_raise(self, agent):
        await agent.cleanup()
