"""Integration tests for EagleAgent.

Tests that verify different components work together correctly.
"""

import pytest
from langchain_core.messages import HumanMessage, AIMessage
import sys
import pathlib

# Ensure app.py is importable
project_root = pathlib.Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.mark.integration
class TestCrossThreadMemory:
    """Test that user profiles persist across different conversation threads."""
    
    async def test_profile_persists_across_threads(self, test_store, test_user_id):
        """Test that profile saved in one context is available in another."""
        from includes.tools.user_profile import create_profile_tools
        
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
        from includes.tools.user_profile import create_profile_tools
        
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
    
    @pytest.mark.asyncio
    async def test_checkpoint_with_user_profile(
        self, test_store, test_user_id, test_thread_id
    ):
        """Test using both checkpoint and store in the same conversation."""
        from includes.tools.user_profile import create_profile_tools
        import app
        import importlib
        importlib.reload(app)
        
        await app.setup_globals()
        
        # Setup: Save user profile
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        await remember_tool.ainvoke({"category": "name", "information": "Alice"})
        
        # Save a checkpoint with conversation state
        config = {"configurable": {"thread_id": test_thread_id, "checkpoint_ns": ""}}
        await app.graph.aupdate_state(config, {"messages": [HumanMessage(content="Hello, remember me?")]}, as_node="model")
        
        state = await app.graph.aget_state(config)
        assert len(state.values["messages"]) == 1
        
        # Load profile
        profile = await test_store.aget(("users",), test_user_id)
        assert profile is not None
        assert profile.value["name"] == "Alice"


@pytest.mark.integration
class TestEndToEndConversation:
    """Test complete conversation flows."""
    
    @pytest.mark.asyncio
    async def test_conversation_with_profile_building(
        self, test_store, test_user_id
    ):
        """Test a realistic conversation that builds a user profile."""
        from includes.tools.user_profile import create_profile_tools
        import app
        import importlib
        importlib.reload(app)
        
        await app.setup_globals()
        
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        thread_id = "conversation-1"
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        
        # Turn 1: User introduces themselves
        await remember_tool.ainvoke({"category": "name", "information": "Bob"})
        
        await app.graph.aupdate_state(config, {
            "messages": [
                HumanMessage(content="Hi, I'm Bob"),
                AIMessage(content="Nice to meet you, Bob!"),
            ]
        }, as_node="model")
        
        # Turn 2: User shares preferences
        await remember_tool.ainvoke({"category": "preferences", "information": "Python"})
        
        await app.graph.aupdate_state(config, {
            "messages": [
                HumanMessage(content="I love Python"),
                AIMessage(content="Great! I've noted that."),
            ]
        }, as_node="model")
        
        # Verify profile was built
        profile = await test_store.aget(("users",), test_user_id)
        assert profile.value["name"] == "Bob"
        assert "Python" in profile.value["preferences"]
        
        # Verify conversation state was saved
        state = await app.graph.aget_state(config)
        assert len(state.values["messages"]) >= 4
    
    @pytest.mark.asyncio
    async def test_resume_conversation_with_profile(
        self, test_store, test_user_id
    ):
        """Test resuming a conversation with existing profile."""
        from includes.tools.user_profile import create_profile_tools
        import app
        import importlib
        importlib.reload(app)
        
        await app.setup_globals()
        
        tools = create_profile_tools(test_store, test_user_id)
        remember_tool = tools[0]
        
        # Setup: Previous conversation built a profile
        await remember_tool.ainvoke({"category": "name", "information": "Charlie"})
        await remember_tool.ainvoke({"category": "preferred_name", "information": "Chuck"})
        
        thread_id = "resume-conversation"
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        
        await app.graph.aupdate_state(config, {
            "messages": [
                HumanMessage(content="Call me Chuck"),
                AIMessage(content="Got it, Chuck!"),
            ]
        }, as_node="model")
        
        # Resume: Load profile and conversation
        profile = await test_store.aget(("users",), test_user_id)
        state = await app.graph.aget_state(config)
        
        # Should have both profile and conversation history
        assert profile.value["name"] == "Charlie"
        assert profile.value["preferred_name"] == "Chuck"
        assert len(state.values["messages"]) >= 2


@pytest.mark.integration
class TestGraphWithStubModel:
    """Test the complete graph with stubbed LLM."""
    
    @pytest.mark.asyncio
    async def test_graph_with_user_profile(self, test_store, test_user_id, test_thread_id, stub_chat_model, monkeypatch):
        """Test that the graph can access user profiles."""
        import sys
        import pathlib
        
        # Ensure app.py is importable
        project_root = pathlib.Path(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
            
        import app
        import importlib
        importlib.reload(app)
        
        await app.setup_globals()
        
        # Patch the model
        import langchain_google_genai
        monkeypatch.setattr(
            langchain_google_genai, "ChatGoogleGenerativeAI", stub_chat_model, raising=True
        )
        
        # Patch the store to use test store
        monkeypatch.setattr(app, "store", test_store)
        
        # Create a user profile
        from includes.tools.user_profile import create_profile_tools
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
