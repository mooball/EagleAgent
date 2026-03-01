"""Tests for user profile management tools.

Tests the LangChain tools that agents use to remember, retrieve,
and forget user information.
"""

import pytest
from user_profile_tools import create_profile_tools


class TestRememberUserInfo:
    """Test the remember_user_info tool."""
    
    async def test_remember_simple_name(self, test_store, test_user_id):
        """Test remembering a user's name."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]  # remember_user_info
        
        # Act
        result = await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        
        # Assert
        assert "remembered" in result.lower()
        
        # Verify it was stored
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["name"] == "Alice"
    
    async def test_remember_preferred_name(self, test_store, test_user_id):
        """Test remembering a user's preferred name."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Act
        result = await remember_tool.ainvoke({
            "category": "preferred_name",
            "information": "Al"
        })
        
        # Assert
        assert "remembered" in result.lower()
        
        # Verify
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["preferred_name"] == "Al"
    
    async def test_remember_preference(self, test_store, test_user_id):
        """Test adding a preference to the list."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Add first preference
        await remember_tool.ainvoke({
            "category": "preferences",
            "information": "Python programming"
        })
        
        # Add second preference
        await remember_tool.ainvoke({
            "category": "preferences",
            "information": "Testing"
        })
        
        # Verify both are in the list
        profile = await test_store.aget(("users",), test_user_id)
        assert "Python programming" in profile.value["preferences"]
        assert "Testing" in profile.value["preferences"]
    
    async def test_remember_fact(self, test_store, test_user_id):
        """Test adding facts about the user."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Add multiple facts
        facts = [
            "works at MooBall",
            "loves automated tests",
            "lives in San Francisco"
        ]
        
        for fact in facts:
            await remember_tool.ainvoke({"category": "facts", "information": fact})
        
        # Verify all facts are stored
        profile = await test_store.aget(("users",), test_user_id)
        for fact in facts:
            assert fact in profile.value["facts"]
    
    async def test_remember_job(self, test_store, test_user_id):
        """Test remembering job information."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        await remember_tool.ainvoke({
            "category": "job",
            "information": "Software Engineer"
        })
        
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["job"] == "Software Engineer"
    
    async def test_remember_location(self, test_store, test_user_id):
        """Test remembering location."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        await remember_tool.ainvoke({
            "category": "location",
            "information": "New York"
        })
        
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["location"] == "New York"
    
    async def test_remember_custom_category(self, test_store, test_user_id):
        """Test storing custom category data."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        await remember_tool.ainvoke({
            "category": "favorite_color",
            "information": "Blue"
        })
        
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["favorite_color"] == "Blue"
    
    async def test_remember_updates_existing_profile(self, test_store, test_user_id):
        """Test that remembering info updates existing profile without overwriting."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Add name
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        
        # Add job
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # Both should be present
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["name"] == "Alice"
        assert profile.value["job"] == "Engineer"
    
    async def test_remember_duplicate_preference_not_added(self, test_store, test_user_id):
        """Test that duplicate preferences are not added."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Add same preference twice
        await remember_tool.ainvoke({
            "category": "preferences",
            "information": "Python"
        })
        await remember_tool.ainvoke({
            "category": "preferences",
            "information": "Python"
        })
        
        # Should only appear once
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["preferences"].count("Python") == 1


class TestGetUserInfo:
    """Test the get_user_info tool."""
    
    async def test_get_all_info(self, test_store, test_user_id):
        """Test retrieving complete user profile."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Setup: Create profile
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # Act: Get all info
        result = await get_tool.ainvoke({})
        
        # Assert
        assert "Alice" in result
        assert "Engineer" in result
    
    async def test_get_specific_category(self, test_store, test_user_id):
        """Test retrieving a specific category."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Setup
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # Act: Get only name
        result = await get_tool.ainvoke({"category": "name"})
        
        # Assert
        assert "Alice" in result
        assert "Engineer" not in result  # Other fields shouldn't be included
    
    async def test_get_preferences_list(self, test_store, test_user_id):
        """Test retrieving preferences as a list."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Setup
        await remember_tool.ainvoke({"category": "preferences", "information": "Python"})
        await remember_tool.ainvoke({"category": "preferences", "information": "Testing"})
        
        # Act
        result = await get_tool.ainvoke({"category": "preferences"})
        
        # Assert
        assert "Python" in result
        assert "Testing" in result
    
    async def test_get_empty_profile(self, test_store, test_user_id):
        """Test retrieving info when no profile exists."""
        tools = create_profile_tools(test_store, test_user_id)
        get_tool = tools[1]
        
        # Act
        result = await get_tool.ainvoke({})
        
        # Assert
        assert "no information" in result.lower() or "nothing stored" in result.lower()
    
    async def test_get_nonexistent_category(self, test_store, test_user_id):
        """Test retrieving a category that doesn't exist."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Setup: Create profile with only name
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        
        # Act: Try to get non-existent category
        result = await get_tool.ainvoke({"category": "favorite_food"})
        
        # Assert
        assert "not" in result.lower() or "no" in result.lower()


class TestForgetUserInfo:
    """Test the forget_user_info tool."""
    
    async def test_forget_specific_field(self, test_store, test_user_id):
        """Test forgetting a specific field."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, _, forget_tool = tools[0], tools[1], tools[2]
        
        # Setup: Create profile
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # Act: Forget job
        result = await forget_tool.ainvoke({"category": "job"})
        
        # Assert
        assert "forgotten" in result.lower() or "removed" in result.lower()
        
        # Verify: Name should still exist, job should not
        profile = await test_store.aget(("users",), test_user_id)
        assert "name" in profile.value
        assert "job" not in profile.value
    
    async def test_forget_nonexistent_field(self, test_store, test_user_id):
        """Test forgetting a field that doesn't exist."""
        tools = create_profile_tools(test_store, test_user_id)
        forget_tool = tools[2]
        
        # Act
        result = await forget_tool.ainvoke({"category": "nonexistent"})
        
        # Assert: Should handle gracefully
        assert result is not None
    
    async def test_forget_from_empty_profile(self, test_store, test_user_id):
        """Test forgetting when no profile exists."""
        tools = create_profile_tools(test_store, test_user_id)
        forget_tool = tools[2]
        
        # Act
        result = await forget_tool.ainvoke({"category": "name"})
        
        # Assert: Should handle gracefully
        assert result is not None


class TestProfileToolsIntegration:
    """Test tools working together in realistic scenarios."""
    
    async def test_complete_profile_lifecycle(self, test_store, test_user_id):
        """Test complete lifecycle: remember, get, update, forget."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool, forget_tool = tools[0], tools[1], tools[2]
        
        # 1. Remember initial info
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # 2. Get info
        info = await get_tool.ainvoke({})
        assert "Alice" in info
        assert "Engineer" in info
        
        # 3. Update job
        await remember_tool.ainvoke({"category": "job", "information": "Senior Engineer"})
        updated_info = await get_tool.ainvoke({"category": "job"})
        assert "Senior Engineer" in updated_info
        
        # 4. Forget job
        await forget_tool.ainvoke({"category": "job"})
        final_info = await get_tool.ainvoke({})
        assert "Alice" in final_info
        assert "Engineer" not in final_info
    
    async def test_building_profile_incrementally(self, test_store, test_user_id):
        """Test building a profile over multiple interactions."""
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Interaction 1: Tell name
        await remember_tool.ainvoke({"category": "name", "information": "Bob"})
        
        # Interaction 2: Tell preferred name
        await remember_tool.ainvoke({"category": "preferred_name", "information": "Bobby"})
        
        # Interaction 3: Share preference
        await remember_tool.ainvoke({"category": "preferences", "information": "AI"})
        
        # Interaction 4: Share another preference
        await remember_tool.ainvoke({"category": "preferences", "information": "ML"})
        
        # Interaction 5: Share fact
        await remember_tool.ainvoke({"category": "facts", "information": "works remotely"})
        
        # Verify complete profile
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["name"] == "Bob"
        assert profile.value["preferred_name"] == "Bobby"
        assert len(profile.value["preferences"]) == 2
        assert len(profile.value["facts"]) == 1
