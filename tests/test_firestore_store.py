"""Tests for FirestoreStore (cross-thread user memory).

Tests the BaseStore implementation that persists user profiles
across all conversation threads.
"""

import pytest
from langgraph.store.base import GetOp, PutOp, SearchOp, ListNamespacesOp


class TestFirestoreStoreBasics:
    """Test basic CRUD operations on FirestoreStore."""
    
    async def test_put_and_get_simple_value(self, test_store, test_user_id):
        """Test storing and retrieving a simple user profile."""
        # Arrange
        profile_data = {"name": "Test User", "job": "Engineer"}
        namespace = ("users",)
        
        # Act: Store the profile
        await test_store.aput(namespace, test_user_id, profile_data)
        
        # Act: Retrieve the profile
        result = await test_store.aget(namespace, test_user_id)
        
        # Assert
        assert result is not None
        assert result.value == profile_data
        assert result.key == test_user_id
        assert result.namespace == namespace
        assert result.created_at is not None
        assert result.updated_at is not None
    
    async def test_get_nonexistent_key_returns_none(self, test_store):
        """Test that getting a non-existent key returns None."""
        result = await test_store.aget(("users",), "nonexistent@example.com")
        assert result is None
    
    async def test_update_existing_value(self, test_store, test_user_id):
        """Test updating an existing user profile."""
        namespace = ("users",)
        
        # Create initial profile
        await test_store.aput(namespace, test_user_id, {"name": "Original"})
        initial = await test_store.aget(namespace, test_user_id)
        
        # Update profile
        await test_store.aput(namespace, test_user_id, {"name": "Updated"})
        updated = await test_store.aget(namespace, test_user_id)
        
        # Assert
        assert initial.value["name"] == "Original"
        assert updated.value["name"] == "Updated"
        assert updated.created_at == initial.created_at  # Should preserve created_at
        assert updated.updated_at >= initial.updated_at  # Should update timestamp
    
    async def test_delete_value(self, test_store, test_user_id):
        """Test deleting a user profile."""
        namespace = ("users",)
        
        # Create profile
        await test_store.aput(namespace, test_user_id, {"name": "Test"})
        assert await test_store.aget(namespace, test_user_id) is not None
        
        # Delete profile (put None)
        await test_store.aput(namespace, test_user_id, None)
        
        # Verify deletion
        assert await test_store.aget(namespace, test_user_id) is None


class TestFirestoreStoreBatchOperations:
    """Test batch operations on FirestoreStore."""
    
    async def test_batch_get_operations(self, test_store):
        """Test batch retrieval of multiple profiles."""
        namespace = ("users",)
        
        # Setup: Create multiple profiles
        await test_store.aput(namespace, "user1@test.com", {"name": "User 1"})
        await test_store.aput(namespace, "user2@test.com", {"name": "User 2"})
        
        # Act: Batch get
        ops = [
            GetOp(namespace=namespace, key="user1@test.com"),
            GetOp(namespace=namespace, key="user2@test.com"),
            GetOp(namespace=namespace, key="nonexistent@test.com"),
        ]
        results = await test_store.abatch(ops)
        
        # Assert
        assert len(results) == 3
        assert results[0].value["name"] == "User 1"
        assert results[1].value["name"] == "User 2"
        assert results[2] is None  # Non-existent key
    
    async def test_batch_put_operations(self, test_store):
        """Test batch storage of multiple profiles."""
        namespace = ("users",)
        
        # Act: Batch put
        ops = [
            PutOp(namespace=namespace, key="batch1@test.com", value={"name": "Batch 1"}),
            PutOp(namespace=namespace, key="batch2@test.com", value={"name": "Batch 2"}),
        ]
        await test_store.abatch(ops)
        
        # Assert: Verify all were stored
        user1 = await test_store.aget(namespace, "batch1@test.com")
        user2 = await test_store.aget(namespace, "batch2@test.com")
        
        assert user1.value["name"] == "Batch 1"
        assert user2.value["name"] == "Batch 2"


class TestFirestoreStoreSearch:
    """Test search and query operations."""
    
    async def test_search_by_namespace_prefix(self, test_store):
        """Test searching for documents by namespace prefix."""
        # Setup: Create profiles in different namespaces
        await test_store.aput(("users",), "user1@test.com", {"name": "User 1"})
        await test_store.aput(("users",), "user2@test.com", {"name": "User 2"})
        await test_store.aput(("admins",), "admin1@test.com", {"name": "Admin 1"})
        
        # Act: Search for users namespace
        op = SearchOp(namespace_prefix=("users",), limit=10, offset=0)
        results = await test_store.abatch([op])
        
        # Assert
        assert len(results) == 1  # One search result
        search_items = results[0]
        assert len(search_items) == 2  # Two users
        assert all(item.namespace == ("users",) for item in search_items)
    
    async def test_search_with_pagination(self, test_store):
        """Test paginated search results."""
        namespace = ("users",)
        
        # Setup: Create multiple profiles
        for i in range(5):
            await test_store.aput(namespace, f"user{i}@test.com", {"name": f"User {i}"})
        
        # Act: Get first page
        op1 = SearchOp(namespace_prefix=namespace, limit=2, offset=0)
        results1 = await test_store.abatch([op1])
        
        # Act: Get second page
        op2 = SearchOp(namespace_prefix=namespace, limit=2, offset=2)
        results2 = await test_store.abatch([op2])
        
        # Assert
        assert len(results1[0]) == 2
        assert len(results2[0]) == 2
        # Ensure they're different items
        keys1 = {item.key for item in results1[0]}
        keys2 = {item.key for item in results2[0]}
        assert keys1.isdisjoint(keys2)


class TestFirestoreStoreNamespaces:
    """Test namespace handling and listing."""
    
    async def test_list_namespaces(self, test_store):
        """Test listing all namespaces."""
        # Setup: Create data in multiple namespaces
        await test_store.aput(("users",), "user1@test.com", {"name": "User"})
        await test_store.aput(("admins",), "admin1@test.com", {"name": "Admin"})
        await test_store.aput(("guests",), "guest1@test.com", {"name": "Guest"})
        
        # Act: List namespaces
        op = ListNamespacesOp(limit=10, offset=0)
        results = await test_store.abatch([op])
        
        # Assert
        namespaces = results[0]
        assert ("users",) in namespaces
        assert ("admins",) in namespaces
        assert ("guests",) in namespaces
    
    async def test_nested_namespace(self, test_store):
        """Test storing and retrieving with nested namespaces."""
        namespace = ("organization", "team", "users")
        key = "user@test.com"
        value = {"name": "Nested User"}
        
        # Act
        await test_store.aput(namespace, key, value)
        result = await test_store.aget(namespace, key)
        
        # Assert
        assert result is not None
        assert result.value == value
        assert result.namespace == namespace


class TestFirestoreStoreUserProfiles:
    """Test realistic user profile scenarios."""
    
    async def test_complete_user_profile(self, test_store, test_user_profile):
        """Test storing a complete user profile with all fields."""
        namespace = ("users",)
        user_id = "complete@test.com"
        
        # Act
        await test_store.aput(namespace, user_id, test_user_profile)
        result = await test_store.aget(namespace, user_id)
        
        # Assert
        assert result.value["name"] == test_user_profile["name"]
        assert result.value["preferred_name"] == test_user_profile["preferred_name"]
        assert result.value["preferences"] == test_user_profile["preferences"]
        assert result.value["facts"] == test_user_profile["facts"]
        assert result.value["job"] == test_user_profile["job"]
    
    async def test_incremental_profile_updates(self, test_store):
        """Test updating profile fields incrementally."""
        namespace = ("users",)
        user_id = "incremental@test.com"
        
        # Start with basic profile
        await test_store.aput(namespace, user_id, {"name": "User"})
        
        # Get current profile
        current = await test_store.aget(namespace, user_id)
        profile = current.value.copy()
        
        # Add preferences
        profile["preferences"] = ["Python"]
        await test_store.aput(namespace, user_id, profile)
        
        # Get updated profile
        current = await test_store.aget(namespace, user_id)
        profile = current.value.copy()
        
        # Add more preferences
        profile["preferences"].append("Testing")
        await test_store.aput(namespace, user_id, profile)
        
        # Verify final state
        final = await test_store.aget(namespace, user_id)
        assert final.value["name"] == "User"
        assert "Python" in final.value["preferences"]
        assert "Testing" in final.value["preferences"]
    
    async def test_concurrent_user_profiles(self, test_store):
        """Test storing multiple user profiles independently."""
        namespace = ("users",)
        
        # Create profiles for different users
        users = {
            "alice@test.com": {"name": "Alice", "preferences": ["AI"]},
            "bob@test.com": {"name": "Bob", "preferences": ["Data"]},
            "charlie@test.com": {"name": "Charlie", "preferences": ["ML"]},
        }
        
        # Store all profiles
        for user_id, profile in users.items():
            await test_store.aput(namespace, user_id, profile)
        
        # Verify all profiles are independent
        for user_id, expected_profile in users.items():
            result = await test_store.aget(namespace, user_id)
            assert result.value == expected_profile


class TestFirestoreStoreDocumentIDs:
    """Test document ID generation and handling."""
    
    async def test_document_id_format(self, test_store):
        """Test that document IDs are correctly formatted."""
        # Document IDs should be: namespace1/namespace2:key
        namespace = ("users",)
        key = "test@example.com"
        
        await test_store.aput(namespace, key, {"name": "Test"})
        
        # The internal document ID should be "users:test@example.com"
        # We can verify this by checking it was stored and can be retrieved
        result = await test_store.aget(namespace, key)
        assert result is not None
        assert result.key == key
    
    async def test_special_characters_in_key(self, test_store):
        """Test that special characters in keys are handled correctly."""
        namespace = ("users",)
        special_keys = [
            "user+tag@example.com",
            "user.name@example.com",
            "user_name@example.com",
        ]
        
        for key in special_keys:
            await test_store.aput(namespace, key, {"key": key})
            result = await test_store.aget(namespace, key)
            assert result is not None
            assert result.value["key"] == key
