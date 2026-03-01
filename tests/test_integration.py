"""Integration tests for EagleAgent.

Tests that verify different components work together correctly.
"""

import pytest
from langchain_core.messages import HumanMessage, AIMessage


@pytest.mark.integration
class TestCrossThreadMemory:
    """Test that user profiles persist across different conversation threads."""
    
    async def test_profile_persists_across_threads(self, test_store, test_user_id):
        """Test that profile saved in one context is available in another."""
        from user_profile_tools import create_profile_tools
        
        # Thread 1: User tells their name
        tools_thread1 = create_profile_tools(test_store, test_user_id)
        remember_tool = tools_thread1[0]
        
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        await remember_tool.ainvoke({"category": "job", "information": "Engineer"})
        
        # Thread 2: Different tools instance (simulating new conversation)
        tools_thread2 = create_profile_tools(test_store, test_user_id)
        get_tool = tools_thread2[1]
        
        # Should be able to retrieve the same profile
        info = await get_tool.ainvoke({})
        assert "Alice" in info
        assert "Engineer" in info
    
    async def test_multiple_users_isolated(self, test_store):
        """Test that different users have isolated profiles."""
        from user_profile_tools import create_profile_tools
        
        users = {
            "alice@test.com": {"name": "Alice", "job": "Engineer"},
            "bob@test.com": {"name": "Bob", "job": "Designer"},
        }
        
        # Each user tells their info
        for user_id, info in users.items():
            tools = create_profile_tools(test_store, user_id)
            remember_tool = tools[0]
            
            await remember_tool.ainvoke({"category": "name", "information": info["name"]})
            await remember_tool.ainvoke({"category": "job", "information": info["job"]})
        
        # Verify each user can only see their own info
        for user_id, expected_info in users.items():
            tools = create_profile_tools(test_store, user_id)
            get_tool = tools[1]
            
            info = await get_tool.ainvoke({})
            assert expected_info["name"] in info
            # Should NOT see other user's info
            for other_id, other_info in users.items():
                if other_id != user_id:
                    assert other_info["name"] not in info


@pytest.mark.integration
class TestCheckpointAndStore:
    """Test that checkpoints and store work together."""
    
    async def test_checkpoint_with_user_profile(
        self, test_checkpointer, test_store, test_user_id, test_thread_id
    ):
        """Test using both checkpoint and store in the same conversation."""
        from user_profile_tools import create_profile_tools
        from datetime import datetime, timezone
        
        # Setup: Save user profile
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        
        # Save a checkpoint with conversation state
        config = {"configurable": {"thread_id": test_thread_id, "checkpoint_ns": ""}}
        checkpoint = {
            "v": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": "checkpoint-with-profile",
            "channel_values": {
                "messages": [
                    HumanMessage(content="Hello, remember me?").dict(),
                ],
                "user_id": test_user_id,
            },
        }
        await test_checkpointer.aput(config=config, checkpoint=checkpoint, metadata={}, new_versions={})
        
        # Load checkpoint
        loaded_checkpoint = await test_checkpointer.aget_tuple(config)
        assert loaded_checkpoint is not None
        
        # Load profile
        profile = await test_store.aget(("users",), test_user_id)
        
        # Both should be available
        assert loaded_checkpoint.checkpoint["channel_values"]["user_id"] == test_user_id
        assert profile.value["name"] == "Alice"


@pytest.mark.integration
class TestEndToEndConversation:
    """Test complete conversation flows."""
    
    async def test_conversation_with_profile_building(
        self, test_store, test_checkpointer, test_user_id
    ):
        """Test a realistic conversation that builds a user profile."""
        from user_profile_tools import create_profile_tools
        from datetime import datetime, timezone
        
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        thread_id = "conversation-1"
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        
        # Turn 1: User introduces themselves
        await remember_tool.ainvoke({"category": "name", "information": "Bob"})
        
        checkpoint1 = {
            "v": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": "c1",
            "channel_values": {
                "messages": [
                    HumanMessage(content="Hi, I'm Bob").dict(),
                    AIMessage(content="Nice to meet you, Bob!").dict(),
                ],
            },
        }
        await test_checkpointer.aput(config=config, checkpoint=checkpoint1, metadata={"step": 1}, new_versions={})
        
        # Turn 2: User shares preferences
        await remember_tool.ainvoke({"category": "preferences", "information": "Python"})
        
        checkpoint2 = {
            "v": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": "c2",
            "channel_values": {
                "messages": [
                    HumanMessage(content="Hi, I'm Bob").dict(),
                    AIMessage(content="Nice to meet you, Bob!").dict(),
                    HumanMessage(content="I love Python").dict(),
                    AIMessage(content="Great! I've noted that.").dict(),
                ],
            },
        }
        await test_checkpointer.aput(config=config, checkpoint=checkpoint2, metadata={"step": 2}, new_versions={})
        
        # Verify profile was built
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["name"] == "Bob"
        assert "Python" in profile.value["preferences"]
        
        # Verify conversation state was saved
        loaded = await test_checkpointer.aget_tuple(config)
        assert len(loaded.checkpoint["channel_values"]["messages"]) == 4
    
    async def test_resume_conversation_with_profile(
        self, test_store, test_checkpointer, test_user_id
    ):
        """Test resuming a conversation with existing profile."""
        from user_profile_tools import create_profile_tools
        from datetime import datetime, timezone
        
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool, get_tool = tools[0], tools[1]
        
        # Setup: Previous conversation built a profile
        await remember_tool.ainvoke({"category": "name", "information": "Charlie"})
        await remember_tool.ainvoke({"category": "preferred_name", "information": "Chuck"})
        
        thread_id = "resume-conversation"
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        
        # Save previous conversation state
        old_checkpoint = {
            "v": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": "old-checkpoint",
            "channel_values": {
                "messages": [
                    HumanMessage(content="Call me Chuck").dict(),
                    AIMessage(content="Got it, Chuck!").dict(),
                ],
            },
        }
        await test_checkpointer.aput(config=config, checkpoint=old_checkpoint, metadata={}, new_versions={})
        
        # Resume: Load profile and conversation
        profile = await test_store.aget(("users",), test_user_id)
        loaded_checkpoint = await test_checkpointer.aget_tuple(config)
        
        # Should have both profile and conversation history
        assert profile.value["name"] == "Charlie"
        assert profile.value["preferred_name"] == "Chuck"
        assert len(loaded_checkpoint.checkpoint["channel_values"]["messages"]) == 2


@pytest.mark.integration
class TestGraphWithStubModel:
    """Test the complete graph with stubbed LLM."""
    
    async def test_graph_with_user_profile(self, test_store, test_user_id, test_thread_id, stub_chat_model, monkeypatch):
        """Test that the graph can access user profiles."""
        import sys
        import pathlib
        
        # Ensure app.py is importable
        project_root = pathlib.Path(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        
        # Patch the model
        import langchain_google_genai
        monkeypatch.setattr(
            langchain_google_genai, "ChatGoogleGenerativeAI", stub_chat_model, raising=True
        )
        
        # Patch the store to use test store
        import app
        monkeypatch.setattr(app, "store", test_store)
        
        # Create a user profile
        from user_profile_tools import create_profile_tools
        tools = create_profile_tools(test_store, test_user_id)
        await tools[0].ainvoke({"category": "name", "information": "Test User"})
        
        # Run the graph
        config = {"configurable": {"thread_id": test_thread_id, "checkpoint_ns": ""}}
        result = await app.graph.ainvoke(
            {
                "messages": [HumanMessage(content="hello")],
                "user_id": test_user_id,
            },
            config=config,
        )
        
        # Should complete successfully
        assert "messages" in result
        assert isinstance(result["messages"][-1], AIMessage)
