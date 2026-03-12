import pytest
from unittest.mock import patch, MagicMock, AsyncMock, Mock
from config.settings import Config
from includes.prompts import build_profile_context, format_profile_section
from langchain_core.tools import BaseTool
from includes.agents import GeneralAgent

class TestConfigRoles:
    def test_get_admin_emails_parsing(self):
        # Save original value to restore later
        original_admin_emails = Config.ADMIN_EMAILS
        
        try:
            # Standard list
            Config.ADMIN_EMAILS = "tom@mooball.net,harry@eagle-exports.com"
            assert Config.get_admin_emails() == ["tom@mooball.net", "harry@eagle-exports.com"]
            
            # With spaces
            Config.ADMIN_EMAILS = " tom@mooball.net , harry@eagle-exports.com "
            assert Config.get_admin_emails() == ["tom@mooball.net", "harry@eagle-exports.com"]
            
            # Mixed case (should lowercase)
            Config.ADMIN_EMAILS = "Tom@Mooball.net,HARRY@eagle-exports.com"
            assert Config.get_admin_emails() == ["tom@mooball.net", "harry@eagle-exports.com"]
            
            # Empty values
            Config.ADMIN_EMAILS = ""
            assert Config.get_admin_emails() == []
            Config.ADMIN_EMAILS = "   ,,  "
            assert Config.get_admin_emails() == []
        finally:
            # Restore original state
            Config.ADMIN_EMAILS = original_admin_emails


class TestPromptsRoles:
    def test_format_profile_section_role(self):
        formatted = format_profile_section("role", "Admin")
        assert formatted == "- Role: Admin"
        
        formatted = format_profile_section("role", "Staff")
        assert formatted == "- Role: Staff"

    def test_build_profile_context_includes_role_with_priority(self):
        profile_data = {
            "preferred_name": "Tom",
            "preferences": ["Dark mode"],
            "role": "Admin",
            "name": "Tom Smith"
        }
        sections = build_profile_context(profile_data)
        
        # Role should be properly injected
        assert "- Role: Admin" in sections
        
        role_index = sections.index("- Role: Admin")
        pref_name_index = sections.index("- Preferred name: Tom (use this to address the user)")
        # Role should have priority and appear before preferred name in the context
        assert role_index < pref_name_index


@pytest.mark.asyncio
class TestAppRoles:
    """Test role-based tool filtering in GeneralAgent.get_tools_async()."""

    def _make_agent(self, admin_only_tools=None):
        mock_model = Mock()
        mock_store = AsyncMock()
        return GeneralAgent(
            model=mock_model,
            store=mock_store,
            mcp_client=None,
            admin_only_tools=admin_only_tools or ["test_admin_tool"],
        )

    @patch('includes.agents.general_agent.config')
    async def test_get_user_tools_admin(self, mock_config):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        agent = self._make_agent()

        tools = await agent.get_tools_async("admin@example.com")
        tool_names = [t.name for t in tools]

        # Admin should keep all tools (no filtering applied)
        assert agent._last_user_role == "Admin"
        # Profile tools are always included
        assert "remember_user_info" in tool_names

    @patch('includes.agents.general_agent.config')
    async def test_get_user_tools_staff(self, mock_config):
        mock_config.get_admin_emails.return_value = ["admin@example.com"]

        # Add a mock MCP tool named "test_admin_tool" so we can verify it's filtered out
        mock_mcp = AsyncMock()
        admin_tool = Mock(spec=BaseTool)
        admin_tool.name = "test_admin_tool"
        normal_mcp_tool = Mock(spec=BaseTool)
        normal_mcp_tool.name = "normal_tool"
        mock_mcp.get_tools = AsyncMock(return_value=[admin_tool, normal_mcp_tool])

        agent = self._make_agent()
        agent.mcp_client = mock_mcp

        tools = await agent.get_tools_async("staff@example.com")
        tool_names = [t.name for t in tools]

        assert agent._last_user_role == "Staff"
        assert "normal_tool" in tool_names
        assert "test_admin_tool" not in tool_names  # Filtered out for staff