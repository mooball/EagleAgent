#!/usr/bin/env python3
"""
Demo script to manage user profiles in the Firestore store.

This script demonstrates how to set, get, and delete user profile information
that persists across conversation threads.

Usage:
    # Set user profile
    uv run manage_user_profile.py set tom@mooball.net name "Tom"
    uv run manage_user_profile.py set tom@mooball.net preferences "loves Python"
    uv run manage_user_profile.py set tom@mooball.net facts "works at MooBall"
    
    # Get user profile
    uv run manage_user_profile.py get tom@mooball.net
    
    # Get specific field
    uv run manage_user_profile.py get tom@mooball.net name
    
    # Delete user profile
    uv run manage_user_profile.py delete tom@mooball.net
"""

import asyncio
import sys
from firestore_store import FirestoreStore


async def set_profile(store: FirestoreStore, user_id: str, field: str, value: str):
    """Set a field in user profile."""
    # Get existing profile
    item = await store.aget(("users",), user_id)
    profile = item.value if item else {}
    
    # Handle special fields (lists)
    if field in ["facts", "preferences"]:
        if field not in profile:
            profile[field] = []
        if value not in profile[field]:
            profile[field].append(value)
    else:
        profile[field] = value
    
    # Save profile
    await store.aput(("users",), user_id, profile)
    print(f"✅ Set {field} = {value} for {user_id}")
    print(f"📝 Current profile: {profile}")


async def get_profile(store: FirestoreStore, user_id: str, field: str = None):
    """Get user profile or specific field."""
    item = await store.aget(("users",), user_id)
    
    if not item:
        print(f"❌ No profile found for {user_id}")
        return
    
    profile = item.value
    
    if field:
        if field in profile:
            print(f"📋 {field}: {profile[field]}")
        else:
            print(f"❌ Field '{field}' not found in profile")
    else:
        print(f"📋 Profile for {user_id}:")
        for key, value in profile.items():
            if isinstance(value, list):
                print(f"  - {key}: {', '.join(str(v) for v in value)}")
            else:
                print(f"  - {key}: {value}")
        print(f"\n🕐 Created: {item.created_at}")
        print(f"🕐 Updated: {item.updated_at}")


async def delete_profile(store: FirestoreStore, user_id: str):
    """Delete user profile."""
    await store.adelete(("users",), user_id)
    print(f"✅ Deleted profile for {user_id}")


async def list_users(store: FirestoreStore):
    """List all user profiles."""
    results = await store.asearch(("users",))
    
    if not results:
        print("❌ No user profiles found")
        return
    
    print(f"📋 Found {len(results)} user profile(s):\n")
    for item in results:
        print(f"👤 {item.key}")
        for key, value in item.value.items():
            if isinstance(value, list):
                print(f"   - {key}: {', '.join(str(v) for v in value)}")
            else:
                print(f"   - {key}: {value}")
        print()


async def main():
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    command = sys.argv[1].lower()
    
    # Initialize store
    store = FirestoreStore(project_id=os.getenv("GOOGLE_PROJECT_ID"), collection="user_memory")
    
    if command == "set":
        if len(sys.argv) != 5:
            print("Usage: manage_user_profile.py set USER_ID FIELD VALUE")
            return
        user_id = sys.argv[2]
        field = sys.argv[3]
        value = sys.argv[4]
        await set_profile(store, user_id, field, value)
    
    elif command == "get":
        if len(sys.argv) < 3:
            print("Usage: manage_user_profile.py get USER_ID [FIELD]")
            return
        user_id = sys.argv[2]
        field = sys.argv[3] if len(sys.argv) > 3 else None
        await get_profile(store, user_id, field)
    
    elif command == "delete":
        if len(sys.argv) != 3:
            print("Usage: manage_user_profile.py delete USER_ID")
            return
        user_id = sys.argv[2]
        await delete_profile(store, user_id)
    
    elif command == "list":
        await list_users(store)
    
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
