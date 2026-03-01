"""
User profile management tools for LangGraph agents.

Provides tools that allow the agent to read and write user profile information
to persistent cross-thread storage.
"""

from typing import Dict, Any, Optional
from langchain_core.tools import tool
from langgraph.store.base import BaseStore


def create_profile_tools(store: BaseStore, user_id: str):
    """
    Create user profile management tools bound to a specific user.
    
    Args:
        store: The BaseStore instance for persistent storage
        user_id: The user's identifier (email)
    
    Returns:
        List of tools the agent can use
    """
    
    @tool
    async def remember_user_info(
        category: str,
        information: str
    ) -> str:
        """
        Remember information about the user for future conversations.
        
        Use this when the user tells you something about themselves that should
        be remembered long-term (across different conversations).
        
        Args:
            category: The category of information (e.g., "name", "preferences", "job", "location", "facts")
            information: The information to remember
        
        Returns:
            Confirmation message
        
        Examples:
            - User: "My name is Tom" -> remember_user_info("name", "Tom")
            - User: "I love Python programming" -> remember_user_info("preferences", "loves Python programming")
            - User: "I work at MooBall" -> remember_user_info("job", "works at MooBall")
        """
        # Get current profile
        profile_item = await store.aget(("users",), user_id)
        profile = profile_item.value if profile_item else {}
        
        # Update the specific category
        if category == "name":
            profile["name"] = information
        elif category == "facts":
            # Append to facts list
            if "facts" not in profile:
                profile["facts"] = []
            if information not in profile["facts"]:
                profile["facts"].append(information)
        elif category == "preferences":
            # Append to preferences
            if "preferences" not in profile:
                profile["preferences"] = []
            if isinstance(profile["preferences"], list):
                if information not in profile["preferences"]:
                    profile["preferences"].append(information)
            else:
                # Convert string to list
                profile["preferences"] = [profile["preferences"], information]
        else:
            # Generic key-value storage
            profile[category] = information
        
        # Save updated profile
        await store.aput(("users",), user_id, profile)
        
        return f"I've remembered that {category}: {information}. I'll recall this in future conversations!"
    
    @tool
    async def get_user_info(
        category: Optional[str] = None
    ) -> str:
        """
        Retrieve remembered information about the user.
        
        Args:
            category: Optional category to retrieve. If None, returns all info.
        
        Returns:
            The user's information
        """
        profile_item = await store.aget(("users",), user_id)
        
        if not profile_item:
            return "No user information stored yet."
        
        profile = profile_item.value
        
        if category:
            if category in profile:
                return f"{category}: {profile[category]}"
            else:
                return f"No information stored for category: {category}"
        else:
            # Return all information
            info_parts = []
            for key, value in profile.items():
                if isinstance(value, list):
                    info_parts.append(f"{key}: {', '.join(str(v) for v in value)}")
                else:
                    info_parts.append(f"{key}: {value}")
            
            return "User information:\n" + "\n".join(info_parts) if info_parts else "No user information stored yet."
    
    @tool
    async def forget_user_info(
        category: str
    ) -> str:
        """
        Forget specific information about the user.
        
        Args:
            category: The category to forget
        
        Returns:
            Confirmation message
        """
        profile_item = await store.aget(("users",), user_id)
        
        if not profile_item:
            return "No user information to forget."
        
        profile = profile_item.value
        
        if category in profile:
            del profile[category]
            await store.aput(("users",), user_id, profile)
            return f"I've forgotten the information about {category}."
        else:
            return f"No information stored for category: {category}"
    
    return [remember_user_info, get_user_info, forget_user_info]
