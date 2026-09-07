"""
Unit tests for includes/actions.py — action registry, dispatcher, and helpers.
"""

import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

from includes.chat.actions import (
    _registry,
    get_actions_for_user,
    get_action,
    dispatch_action,
    is_help_request,
    send_action_buttons,
    register_action,
)


# ============================================================================
# Registry tests
# ============================================================================

class TestActionRegistry:
    """Test the action registry and lookup."""

    def test_builtin_actions_registered(self):
        """Built-in new_conversation should be present."""
        assert "new_conversation" in _registry

    def test_get_action_returns_action(self):
        action = get_action("new_conversation")
        assert action is not None
        assert action.name == "new_conversation"
        assert action.admin_only is False

    def test_get_action_unknown_returns_none(self):
        assert get_action("nonexistent_action") is None

    def test_research_actions_are_admin_only(self):
        action = get_action("research_product_info")
        assert action is not None
        assert action.admin_only is True


# ============================================================================
# Role filtering
# ============================================================================

class TestRoleFiltering:
    """Test that get_actions_for_user filters by role correctly."""

    @patch("includes.chat.actions.config")
    def test_non_admin_sees_only_public_actions(self, mock_config):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        actions = get_actions_for_user("staff@example.com")
        names = [a.name for a in actions]
        assert "new_conversation" in names
        assert "research_product_info" not in names

    @patch("includes.chat.actions.config")
    def test_admin_sees_all_actions(self, mock_config):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        actions = get_actions_for_user("admin@example.com")
        names = [a.name for a in actions]
        assert "new_conversation" in names
        assert "research_product_info" in names

    @patch("includes.chat.actions.config")
    def test_empty_user_id_sees_public_only(self, mock_config):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        actions = get_actions_for_user("")
        names = [a.name for a in actions]
        assert "new_conversation" in names
        assert "research_product_info" not in names


# ============================================================================
# is_help_request
# ============================================================================

class TestIsHelpRequest:
    """Test the help-phrase detection."""

    @pytest.mark.parametrize("phrase", [
        "help", "Help", "HELP",
        "actions", "Actions",
        "commands", "menu",
        "show actions",
        "what can i do",
        "help?", "actions!", "menu.",
    ])
    def test_recognized_phrases(self, phrase):
        assert is_help_request(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "help me find a product",
        "what can you do",
        "hello",
        "show me products",
        "",
    ])
    def test_unrecognized_phrases(self, phrase):
        assert is_help_request(phrase) is False


# ============================================================================
# Dispatcher
# ============================================================================

class TestDispatchAction:
    """Test the action dispatcher including role checks."""
    async def test_dispatch_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown action"):
            await dispatch_action("does_not_exist")
    @patch("includes.chat.actions.config")
    async def test_dispatch_admin_action_denied_for_staff(self, mock_config, make_chat_ctx):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        ctx = make_chat_ctx(user_email="staff@example.com")

        await dispatch_action("research_product_info", ctx)

        assert len(ctx.messages) == 1
        assert "permission" in ctx.texts[0].lower()
        assert ctx.get("intent_context") is None

    @patch("includes.chat.actions.config")
    async def test_dispatch_admin_action_allowed_for_admin(self, mock_config, make_chat_ctx):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        ctx = make_chat_ctx(user_email="admin@example.com")

        await dispatch_action("research_product_info", ctx)

        assert "permission" not in ctx.texts[0].lower()
        assert ctx.get("intent_context")

    async def test_dispatch_falls_back_to_the_bound_context(self, bound_chat_ctx):
        await dispatch_action("new_conversation")
        assert len(bound_chat_ctx.messages) == 1
        assert "reset" in bound_chat_ctx.texts[0].lower()

    async def test_new_conversation_sets_a_fresh_thread_id(self, chat_ctx):
        await dispatch_action("new_conversation", chat_ctx)
        new_thread = chat_ctx.get("thread_id")
        assert new_thread and new_thread != chat_ctx.thread_id


# ============================================================================
# Action tools
# ============================================================================

class TestActionTools:
    """Test the LangGraph tool wrappers."""

    def test_create_action_tools_returns_expected_tools(self):
        from includes.tools.action_tools import create_action_tools
        tools = create_action_tools("user@example.com")
        names = [t.name for t in tools]
        assert names == ["list_available_actions", "start_new_conversation"]
    @patch("includes.chat.actions.config")
    async def test_list_available_actions_tool(self, mock_config):
        mock_config.get_admin_emails.return_value = []
        from includes.tools.action_tools import create_action_tools
        tools = create_action_tools("user@example.com")
        list_tool = next(t for t in tools if t.name == "list_available_actions")
        result = await list_tool.ainvoke({})
        assert "New Conversation" in result
        # Non-admin should not see delete
        assert "Delete All" not in result


# ============================================================================
# Cancel handlers (P5 ports from Chainlit callbacks)
# ============================================================================

class TestCancelHandlers:
    async def test_cancel_run_script_messages_cancel(self, make_chat_ctx):
        ctx = make_chat_ctx()
        await get_action("cancel_run_script").handler(
            ctx, payload={"script_name": "update_embeddings"}
        )
        assert any("update_embeddings" in m and "Cancelled" in m for m in ctx.texts)

    async def test_cancel_job_cancels_and_confirms(self, make_chat_ctx):
        ctx = make_chat_ctx()
        job = MagicMock()
        job.id = "abcd1234-abcd-abcd-abcd-abcd1234abcd"
        job.script_name = "sync_net_suite"
        cancel = AsyncMock(return_value=job)
        with patch("includes.graph.job_runner.cancel", new=cancel):
            await get_action("cancel_job").handler(ctx, payload={"job_id": "j1"})
        cancel.assert_awaited_once_with("j1")
        assert any("abcd1234" in m and "Cancelled job" in m for m in ctx.texts)

    async def test_cancel_job_unknown_job_messages_error(self, make_chat_ctx):
        ctx = make_chat_ctx()
        cancel = AsyncMock(side_effect=ValueError("no such job"))
        with patch("includes.graph.job_runner.cancel", new=cancel):
            await get_action("cancel_job").handler(ctx, payload={"job_id": "j1"})
        assert any("Could not cancel" in m for m in ctx.texts)
