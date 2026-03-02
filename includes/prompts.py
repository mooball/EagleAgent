"""
Centralized prompt templates and agent configuration for EagleAgent.

This module contains all system prompts, agent identity configuration,
tool instructions, and user profile context templates. The structure
uses dictionaries that map directly to YAML format for easy future migration.

Future Migration Path:
    To migrate to YAML configuration, simply copy the dictionary structures
    below into config/prompts.yaml and load with yaml.safe_load().
    See config/prompts.yaml.example for the YAML equivalent.

Architecture:
    - AGENT_CONFIG: Core agent identity (name, role, personality)
    - TOOL_INSTRUCTIONS: Guidance for specific tool usage
    - PROFILE_TEMPLATES: Templates for user profile context sections
    - Helper functions: build_system_prompt(), format_profile_section()
"""

from typing import Optional, Dict, Any, List

# =============================================================================
# AGENT CONFIGURATION
# =============================================================================
# Define the core identity, role, and behavior of the EagleAgent.
# This is the foundation of how the agent presents itself to users.

AGENT_CONFIG = {
    "name": "EagleAgent",
    "role": "AI Assistant",
    "description": "A helpful AI assistant powered by Google Gemini with cross-thread memory capabilities",
    
    "personality": {
        "traits": [
            "Helpful and friendly",
            "Professional yet approachable",
            "Attentive to user preferences",
            "Proactive in remembering user information"
        ],
        "tone": "conversational and supportive"
    },
    
    "capabilities": [
        "Remember user preferences across conversations",
        "Personalize responses based on user profile",
        "Learn and recall user-specific facts",
        "Address users by their preferred name"
    ],
    
    "behavior_guidelines": [
        "Always use the user's preferred name when known",
        "Be proactive in learning about the user",
        "Save important user information for future reference",
        "Maintain context across multiple conversation threads"
    ]
}

# =============================================================================
# TOOL INSTRUCTIONS
# =============================================================================
# Instructions for specific tool usage that should be included in system prompts.

TOOL_INSTRUCTIONS = {
    "remember_user_info": {
        "description": "Save user information for future conversations",
        "when_to_use": [
            "When the user tells you information about themselves",
            "When the user says 'call me X' or 'I prefer X'",
            "When the user shares preferences, facts, or personal details"
        ],
        "categories": {
            "preferred_name": "Use this category when user says 'call me X' or 'I prefer X'",
            "preferences": "Use for user's likes, dislikes, or preferences",
            "facts": "Use for biographical information or facts about the user"
        },
        "prompt_template": """When the user tells you information about themselves, use the remember_user_info tool to save it for future conversations. If they say 'call me X' or 'I prefer X', use the 'preferred_name' category."""
    }
}

# =============================================================================
# PROFILE CONTEXT TEMPLATES
# =============================================================================
# Templates for formatting user profile information in system prompts.

PROFILE_TEMPLATES = {
    "header": "User profile information:",
    
    "sections": {
        "preferred_name": "- Preferred name: {preferred_name} (use this to address the user)",
        "name": "- Name: {name}",
        "preferences": "- Preferences: {preferences}",
        "facts": "- Facts: {facts}"
    },
    
    "empty_profile_message": "No user profile information available yet."
}

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def format_profile_section(key: str, value: Any) -> str:
    """
    Format a single profile section with appropriate handling for different data types.
    
    Args:
        key: The profile field name (e.g., 'preferences', 'facts')
        value: The profile field value (can be string, list, dict, etc.)
    
    Returns:
        Formatted string for this profile section
    
    Examples:
        >>> format_profile_section('preferences', ['Python', 'AI'])
        '- Preferences: Python, AI'
        
        >>> format_profile_section('preferred_name', 'Tommy')
        '- Preferred name: Tommy (use this to address the user)'
    """
    # Get the template for this section
    template = PROFILE_TEMPLATES["sections"].get(key)
    if not template:
        # Fallback for unknown keys
        template = f"- {key.replace('_', ' ').title()}: {{value}}"
        return template.format(value=value)
    
    # Handle different value types
    if isinstance(value, list):
        # Join list items with commas
        formatted_value = ", ".join(str(item) for item in value)
        # For list fields, we need to replace the placeholder
        # Template might be "- Preferences: {preferences}"
        # We want to inject the joined list
        return template.format(**{key: formatted_value})
    elif isinstance(value, dict):
        # Format dict as key: value pairs
        formatted_value = ", ".join(f"{k}: {v}" for k, v in value.items())
        return template.format(**{key: formatted_value})
    else:
        # String or other simple type
        return template.format(**{key: value})


def build_profile_context(profile_data: Dict[str, Any]) -> List[str]:
    """
    Build user profile context sections from profile data.
    
    Args:
        profile_data: Dictionary containing user profile information
    
    Returns:
        List of formatted profile context strings
    
    Examples:
        >>> profile = {"preferred_name": "Tom", "facts": ["loves Python"]}
        >>> build_profile_context(profile)
        ['- Preferred name: Tom (use this to address the user)', '- Facts: loves Python']
    """
    sections = []
    
    # Priority order for profile fields
    # preferred_name takes precedence over name
    if "preferred_name" in profile_data:
        sections.append(format_profile_section("preferred_name", profile_data["preferred_name"]))
    elif "name" in profile_data:
        sections.append(format_profile_section("name", profile_data["name"]))
    
    # Add other profile sections
    for key in ["preferences", "facts"]:
        if key in profile_data:
            sections.append(format_profile_section(key, profile_data[key]))
    
    return sections


def build_system_prompt(profile_data: Optional[Dict[str, Any]] = None) -> str:
    """
    Build the complete system prompt for the agent.
    
    This function constructs the system message that provides context to the LLM,
    including agent identity, user profile information (if available),
    and tool usage instructions.
    
    Args:
        profile_data: Optional dictionary containing user profile information.
                     If None, only agent identity and tool instructions are included.
    
    Returns:
        Complete system prompt string ready to be used in a SystemMessage
    
    Examples:
        >>> # With profile data
        >>> profile = {"preferred_name": "Tom", "preferences": ["Python", "AI"]}
        >>> prompt = build_system_prompt(profile)
        >>> "EagleAgent" in prompt
        True
        >>> "Tom" in prompt
        True
        >>> "remember_user_info" in prompt
        True
        
        >>> # Without profile data (new user)
        >>> prompt = build_system_prompt(None)
        >>> "EagleAgent" in prompt
        True
        >>> "remember_user_info" in prompt
        True
    """
    parts = []
    
    # Always add agent identity at the start
    agent_identity = get_agent_identity_prompt()
    if agent_identity:
        parts.append(agent_identity)
        parts.append("")  # Blank line after identity
    
    # Build user profile section if available
    if profile_data:
        parts.append(PROFILE_TEMPLATES["header"])
        
        # Add formatted profile sections
        profile_sections = build_profile_context(profile_data)
        parts.extend(profile_sections)
        
        # Add spacing before tool instructions
        parts.append("")
    
    # Always add tool instructions
    parts.append(TOOL_INSTRUCTIONS["remember_user_info"]["prompt_template"])
    
    return "\n".join(parts)


def get_agent_identity_prompt() -> Optional[str]:
    """
    Build the agent identity prompt from AGENT_CONFIG.
    
    This gives the agent a clear sense of identity and purpose, ensuring
    it responds appropriately when asked about its name, role, or capabilities.
    
    Returns:
        Agent identity prompt string
    
    Example:
        You are EagleAgent, a AI Assistant.
        You are helpful and friendly, professional yet approachable.
        
        Your capabilities include:
        - Remember user preferences across conversations
        - Personalize responses based on user profile
        ...
    """
    
    parts = [
        f"You are {AGENT_CONFIG['name']}, a {AGENT_CONFIG['role']}.",
        f"You are {', '.join(AGENT_CONFIG['personality']['traits'][:2])}.",
        "",
        "Your capabilities include:"
    ]
    
    for capability in AGENT_CONFIG['capabilities']:
        parts.append(f"- {capability}")
    
    parts.append("")
    parts.append("Behavior guidelines:")
    
    for guideline in AGENT_CONFIG['behavior_guidelines']:
        parts.append(f"- {guideline}")
    
    return "\n".join(parts)


# =============================================================================
# CONFIGURATION VALIDATION
# =============================================================================

def validate_config() -> bool:
    """
    Validate that all required configuration is present and well-formed.
    
    Returns:
        True if configuration is valid
    
    Raises:
        ValueError: If configuration is invalid
    """
    # Check AGENT_CONFIG
    required_agent_fields = ["name", "role", "description", "personality", "capabilities", "behavior_guidelines"]
    for field in required_agent_fields:
        if field not in AGENT_CONFIG:
            raise ValueError(f"Missing required field in AGENT_CONFIG: {field}")
    
    # Check TOOL_INSTRUCTIONS
    if "remember_user_info" not in TOOL_INSTRUCTIONS:
        raise ValueError("Missing remember_user_info in TOOL_INSTRUCTIONS")
    
    if "prompt_template" not in TOOL_INSTRUCTIONS["remember_user_info"]:
        raise ValueError("Missing prompt_template in remember_user_info instructions")
    
    # Check PROFILE_TEMPLATES
    required_template_fields = ["header", "sections"]
    for field in required_template_fields:
        if field not in PROFILE_TEMPLATES:
            raise ValueError(f"Missing required field in PROFILE_TEMPLATES: {field}")
    
    return True


# Validate configuration on module load
validate_config()
