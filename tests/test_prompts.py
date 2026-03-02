"""
Unit tests for includes/prompts.py - Centralized prompt configuration.

These tests validate prompt construction, profile context formatting,
and configuration validation.
"""

import pytest
from includes.prompts import (
    build_system_prompt,
    build_profile_context,
    format_profile_section,
    get_agent_identity_prompt,
    validate_config,
    AGENT_CONFIG,
    TOOL_INSTRUCTIONS,
    PROFILE_TEMPLATES
)


class TestFormatProfileSection:
    """Test individual profile section formatting."""
    
    def test_preferred_name_formatting(self):
        """Test that preferred_name includes usage instruction."""
        result = format_profile_section("preferred_name", "Tom")
        assert "Tom" in result
        assert "Preferred name:" in result
        assert "use this to address the user" in result
    
    def test_name_formatting(self):
        """Test that regular name is formatted correctly."""
        result = format_profile_section("name", "Thomas")
        assert "Thomas" in result
        assert "Name:" in result
        assert "use this to address" not in result  # No instruction for regular name
    
    def test_preferences_as_list(self):
        """Test preferences with list value."""
        result = format_profile_section("preferences", ["Python", "AI", "LangGraph"])
        assert "Preferences:" in result
        assert "Python, AI, LangGraph" in result
    
    def test_preferences_as_string(self):
        """Test preferences with string value."""
        result = format_profile_section("preferences", "Python programming")
        assert "Preferences:" in result
        assert "Python programming" in result
    
    def test_facts_as_list(self):
        """Test facts with list value."""
        result = format_profile_section("facts", ["Software engineer", "Loves AI"])
        assert "Facts:" in result
        assert "Software engineer, Loves AI" in result
    
    def test_facts_as_string(self):
        """Test facts with string value."""
        result = format_profile_section("facts", "Works at tech company")
        assert "Facts:" in result
        assert "Works at tech company" in result
    
    def test_preferences_as_dict(self):
        """Test preferences with dict value."""
        result = format_profile_section("preferences", {"language": "Python", "framework": "LangGraph"})
        assert "Preferences:" in result
        assert "language: Python" in result
        assert "framework: LangGraph" in result
    
    def test_unknown_key_fallback(self):
        """Test that unknown keys get a reasonable fallback template."""
        result = format_profile_section("custom_field", "some value")
        assert "Custom Field:" in result  # Title case conversion
        assert "some value" in result
    
    def test_empty_list(self):
        """Test handling of empty list."""
        result = format_profile_section("preferences", [])
        assert "Preferences:" in result
        # Should handle gracefully (empty string or minimal formatting)


class TestBuildProfileContext:
    """Test profile context building from profile data."""
    
    def test_full_profile_with_preferred_name(self):
        """Test complete profile with preferred name."""
        profile = {
            "preferred_name": "Tom",
            "name": "Thomas",  # Should be ignored when preferred_name exists
            "preferences": ["Python", "AI"],
            "facts": ["Software engineer"]
        }
        sections = build_profile_context(profile)
        
        # Should have 3 sections: preferred_name, preferences, facts
        assert len(sections) == 3
        assert any("Tom" in s and "Preferred name:" in s for s in sections)
        assert not any("Thomas" in s for s in sections)  # name should be skipped
        assert any("Python, AI" in s for s in sections)
        assert any("Software engineer" in s for s in sections)
    
    def test_profile_with_name_only(self):
        """Test profile with regular name (no preferred name)."""
        profile = {
            "name": "Thomas",
            "preferences": ["Python"]
        }
        sections = build_profile_context(profile)
        
        # Should use name as fallback
        assert any("Thomas" in s and "Name:" in s for s in sections)
        assert not any("Preferred name:" in s for s in sections)
    
    def test_minimal_profile(self):
        """Test profile with only preferred name."""
        profile = {"preferred_name": "T"}
        sections = build_profile_context(profile)
        
        assert len(sections) == 1
        assert "T" in sections[0]
        assert "Preferred name:" in sections[0]
    
    def test_empty_profile(self):
        """Test empty profile dictionary."""
        profile = {}
        sections = build_profile_context(profile)
        
        assert len(sections) == 0
    
    def test_profile_with_only_facts(self):
        """Test profile with facts but no name."""
        profile = {"facts": ["Likes Python", "Works in AI"]}
        sections = build_profile_context(profile)
        
        assert len(sections) == 1
        assert "Likes Python, Works in AI" in sections[0]
    
    def test_profile_priority_order(self):
        """Test that preferred_name takes priority over name."""
        profile = {
            "name": "Should not appear",
            "preferred_name": "Should appear"
        }
        sections = build_profile_context(profile)
        
        # Should only have preferred_name, not regular name
        assert len(sections) == 1
        assert "Should appear" in sections[0]
        assert "Should not appear" not in str(sections)


class TestBuildSystemPrompt:
    """Test complete system prompt construction."""
    
    def test_system_prompt_with_full_profile(self):
        """Test system prompt with complete profile data."""
        profile = {
            "preferred_name": "Tom",
            "preferences": ["Python", "concise explanations"],
            "facts": ["Software engineer"]
        }
        prompt = build_system_prompt(profile)
        
        # Should include agent identity
        assert "EagleAgent" in prompt
        assert "AI Assistant" in prompt
        
        # Should include profile header
        assert "User profile information:" in prompt
        
        # Should include all profile sections
        assert "Tom" in prompt
        assert "Python" in prompt
        assert "concise explanations" in prompt
        assert "Software engineer" in prompt
        
        # Should include tool instructions
        assert "remember_user_info" in prompt
        assert "preferred_name" in prompt  # Category name in instructions
    
    def test_system_prompt_with_minimal_profile(self):
        """Test system prompt with minimal profile."""
        profile = {"preferred_name": "T"}
        prompt = build_system_prompt(profile)
        
        # Should include agent identity
        assert "EagleAgent" in prompt
        
        # Should include profile info
        assert "User profile information:" in prompt
        assert "T" in prompt
        
        # Should include tool instructions
        assert "remember_user_info" in prompt
    
    def test_system_prompt_with_none_profile(self):
        """Test system prompt for new user with no profile."""
        prompt = build_system_prompt(None)
        
        # Should include agent identity
        assert "EagleAgent" in prompt
        assert "AI Assistant" in prompt
        
        # Should NOT include profile header (no profile data)
        assert "User profile information:" not in prompt
        
        # Should still include tool instructions
        assert "remember_user_info" in prompt
        assert "call me X" in prompt
    
    def test_system_prompt_with_empty_profile(self):
        """Test system prompt with empty dict."""
        prompt = build_system_prompt({})
        
        # Should include agent identity
        assert "EagleAgent" in prompt
        
        # Empty profile should be treated like no profile
        # (no profile header, just agent identity and tool instructions)
        assert "remember_user_info" in prompt
    
    def test_profile_and_instructions_separated(self):
        """Test that sections are properly separated with blank lines."""
        profile = {"preferred_name": "Tom"}
        prompt = build_system_prompt(profile)
        
        # Should have blank lines between sections
        lines = prompt.split("\n")
        assert "" in lines  # Blank line exists
        
        # Agent identity should come first
        agent_line_idx = next(i for i, line in enumerate(lines) if "EagleAgent" in line)
        
        # Profile should come after agent identity
        profile_line_idx = next(i for i, line in enumerate(lines) if "Tom" in line)
        
        # Instructions should come after profile
        instructions_line_idx = next(i for i, line in enumerate(lines) if "remember_user_info" in line)
        
        assert agent_line_idx < profile_line_idx < instructions_line_idx
    
    def test_system_prompt_instructions_always_present(self):
        """Test that agent identity and tool instructions appear in all cases."""
        # With profile
        prompt_with_profile = build_system_prompt({"preferred_name": "Tom"})
        assert "EagleAgent" in prompt_with_profile
        assert "remember_user_info" in prompt_with_profile
        
        # Without profile
        prompt_without_profile = build_system_prompt(None)
        assert "EagleAgent" in prompt_without_profile
        assert "remember_user_info" in prompt_without_profile
        
        # Empty profile
        prompt_empty = build_system_prompt({})
        assert "EagleAgent" in prompt_empty
        assert "remember_user_info" in prompt_empty


class TestGetAgentIdentityPrompt:
    """Test agent identity prompt generation (future feature)."""
    
    def test_agent_identity_includes_name_and_role(self):
        """Test that agent identity prompt includes basic info."""
        identity = get_agent_identity_prompt()
        
        assert identity is not None
        assert AGENT_CONFIG["name"] in identity
        assert AGENT_CONFIG["role"] in identity
    
    def test_agent_identity_includes_capabilities(self):
        """Test that capabilities are listed."""
        identity = get_agent_identity_prompt()
        
        # At least one capability should be mentioned
        assert any(cap in identity for cap in AGENT_CONFIG["capabilities"])
    
    def test_agent_identity_includes_guidelines(self):
        """Test that behavior guidelines are included."""
        identity = get_agent_identity_prompt()
        
        # At least one guideline should be mentioned
        assert any(guideline in identity for guideline in AGENT_CONFIG["behavior_guidelines"])


class TestConfigValidation:
    """Test configuration validation."""
    
    def test_validate_config_passes(self):
        """Test that current config is valid."""
        # Should not raise any exceptions
        assert validate_config() is True
    
    def test_agent_config_has_required_fields(self):
        """Test that AGENT_CONFIG has all required fields."""
        required_fields = ["name", "role", "description", "personality", "capabilities", "behavior_guidelines"]
        
        for field in required_fields:
            assert field in AGENT_CONFIG, f"Missing required field: {field}"
    
    def test_tool_instructions_has_remember_user_info(self):
        """Test that tool instructions for remember_user_info exist."""
        assert "remember_user_info" in TOOL_INSTRUCTIONS
        assert "prompt_template" in TOOL_INSTRUCTIONS["remember_user_info"]
    
    def test_profile_templates_has_required_fields(self):
        """Test that PROFILE_TEMPLATES has required fields."""
        assert "header" in PROFILE_TEMPLATES
        assert "sections" in PROFILE_TEMPLATES
        
        # Check that common sections exist
        assert "preferred_name" in PROFILE_TEMPLATES["sections"]
        assert "name" in PROFILE_TEMPLATES["sections"]
        assert "preferences" in PROFILE_TEMPLATES["sections"]
        assert "facts" in PROFILE_TEMPLATES["sections"]


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_profile_with_none_values(self):
        """Test handling of None values in profile."""
        profile = {
            "preferred_name": None,
            "preferences": None
        }
        # Should not crash
        sections = build_profile_context(profile)
        # Sections with None values should be skipped or handled gracefully
    
    def test_profile_with_numeric_values(self):
        """Test handling of numeric values."""
        profile = {
            "preferred_name": "User123",
            "facts": [42, "meaning of life"]
        }
        sections = build_profile_context(profile)
        
        # Should convert numbers to strings
        assert any("42" in s for s in sections)
    
    def test_very_long_profile_values(self):
        """Test handling of very long profile values."""
        long_list = ["item" + str(i) for i in range(100)]
        profile = {"preferences": long_list}
        
        prompt = build_system_prompt(profile)
        # Should not crash, should handle gracefully
        assert prompt is not None
        assert len(prompt) > 0
    
    def test_profile_with_special_characters(self):
        """Test profile values with special characters."""
        profile = {
            "preferred_name": "Tom's Nickname",
            "facts": ['Likes "AI" & ML']
        }
        prompt = build_system_prompt(profile)
        
        # Should preserve special characters
        assert "Tom's Nickname" in prompt
        assert '"AI"' in prompt or "AI" in prompt  # Might be escaped


class TestRealWorldScenarios:
    """Test realistic usage scenarios."""
    
    def test_new_user_first_interaction(self):
        """Simulate new user with no profile."""
        prompt = build_system_prompt(None)
        
        # Should introduce agent identity
        assert "EagleAgent" in prompt
        
        # Should encourage user to share information
        assert "remember_user_info" in prompt
        assert "call me" in prompt.lower()
    
    def test_returning_user_with_preferences(self):
        """Simulate returning user with established profile."""
        profile = {
            "preferred_name": "Dr. Smith",
            "preferences": ["formal tone", "technical depth"],
            "facts": ["PhD in CS", "works at university"]
        }
        prompt = build_system_prompt(profile)
        
        # Should include agent identity
        assert "EagleAgent" in prompt
        
        # Should include personalization
        assert "Dr. Smith" in prompt
        assert "formal tone" in prompt
        assert "PhD in CS" in prompt
        
        # Still should have tool instructions for updates
        assert "remember_user_info" in prompt
    
    def test_user_updates_preferred_name(self):
        """Simulate user changing their preferred name."""
        # Initial profile
        old_profile = {"preferred_name": "Tommy", "facts": ["engineer"]}
        old_prompt = build_system_prompt(old_profile)
        assert "Tommy" in old_prompt
        
        # Updated profile
        new_profile = {"preferred_name": "Tom", "facts": ["engineer"]}
        new_prompt = build_system_prompt(new_profile)
        assert "Tom" in new_prompt
        assert "Tommy" not in new_prompt
    
    def test_gradual_profile_building(self):
        """Simulate user gradually building their profile over time."""
        # Start with just name
        profile_v1 = {"preferred_name": "Alice"}
        prompt_v1 = build_system_prompt(profile_v1)
        assert "Alice" in prompt_v1
        
        # Add preferences
        profile_v2 = {"preferred_name": "Alice", "preferences": ["Python"]}
        prompt_v2 = build_system_prompt(profile_v2)
        assert "Alice" in prompt_v2
        assert "Python" in prompt_v2
        
        # Add facts
        profile_v3 = {
            "preferred_name": "Alice",
            "preferences": ["Python"],
            "facts": ["Data scientist"]
        }
        prompt_v3 = build_system_prompt(profile_v3)
        assert "Alice" in prompt_v3
        assert "Python" in prompt_v3
        assert "Data scientist" in prompt_v3


class TestConsistency:
    """Test consistency of prompt generation."""
    
    def test_same_input_same_output(self):
        """Test that same profile produces same prompt."""
        profile = {"preferred_name": "Test", "facts": ["A", "B"]}
        
        prompt1 = build_system_prompt(profile)
        prompt2 = build_system_prompt(profile)
        
        assert prompt1 == prompt2
    
    def test_none_vs_empty_dict(self):
        """Test difference between None and empty dict."""
        prompt_none = build_system_prompt(None)
        prompt_empty = build_system_prompt({})
        
        # Both should produce similar output (no profile section)
        # but might be implemented differently
        assert "remember_user_info" in prompt_none
        assert "remember_user_info" in prompt_empty


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
