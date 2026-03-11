import pytest
from unittest.mock import patch, MagicMock
from config.settings import Config
from includes.prompts import build_profile_context, format_profile_section
import app

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
    @patch('app.config')
    @patch('app.store')
    @patch('app.mcp_client', new=None)
    async def test_get_user_tools_admin(self, mock_store, mock_config):
        # Setup mock admin list
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        
        original_admin_tools = app.ADMIN_ONLY_TOOLS
        app.ADMIN_ONLY_TOOLS = ["test_admin_tool"]
        
        try:
            # Mock create_profile_tools to return generic tools + an admin tool
            mock_profile_tool = MagicMock()
            mock_profile_tool.name = "normal_tool"
            
            mock_admin_tool = MagicMock()
            mock_admin_tool.name = "test_admin_tool"
            
            with patch('app.create_profile_tools', return_value=[mock_profile_tool, mock_admin_tool]):
                # Test Admin user
                tools, role = await app.get_user_tools("admin@example.com")
                
                assert role == "Admin"
                tool_names = [getattr(t, "name", "") for t in tools if hasattr(t, "name")]
                assert "normal_tool" in tool_names
                assert "test_admin_tool" in tool_names
        finally:
            app.ADMIN_ONLY_TOOLS = original_admin_tools

    @patch('app.config')
    @patch('app.store')
    @patch('app.mcp_client', new=None)
    async def test_get_user_tools_staff(self, mock_store, mock_config):
        # Setup mock admin list
        mock_config.get_admin_emails.return_value = ["admin@example.com"]
        
        original_admin_tools = app.ADMIN_ONLY_TOOLS
        app.ADMIN_ONLY_TOOLS = ["test_admin_tool"]
        
        try:
            # Mock create_profile_tools to return generic tools + an admin tool
            mock_profile_tool = MagicMock()
            mock_profile_tool.name = "normal_tool"
            
            mock_admin_tool = MagicMock()
            mock_admin_tool.name = "test_admin_tool"
            
            with patch('app.create_profile_tools', return_value=[mock_profile_tool, mock_admin_tool]):
                # Test Staff user
                tools, role = await app.get_user_tools("staff@example.com")
                
                assert role == "Staff"
                tool_names = [getattr(t, "name", "") for t in tools if hasattr(t, "name")]
                
                assert "normal_tool" in tool_names
                assert "test_admin_tool" not in tool_names  # Filtered out
        finally:
            app.ADMIN_ONLY_TOOLS = original_admin_tools