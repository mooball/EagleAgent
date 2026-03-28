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

import datetime
from typing import Optional, Dict, Any, List

# =============================================================================
# AGENT CONFIGURATION
# =============================================================================
# Define the core identity, role, and behavior of the EagleAgent.
# This is the foundation of how the agent presents itself to users.

AGENT_CONFIG = {
    "name": "EagleAgent",
    "role": "Product procurement Assistant",
    "description": "A friendly assistant that helps staff find and purchase products from various suppliers. You work for Eagle Exports which is a company that procures products for customers. You are an expert at finding the best products and prices, and you always remember user preferences to provide personalized recommendations. You have access to various internal databases to find product information based on previous purchases. You are professional yet approachable, and always attentive to user preferences.",
    
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
    
    "company_info": {
        "name": "Eagle Exports",
        "website": "https://www.eaglexp.com.au/",
        "phone": "+61 7 3217 0050",
        "email": "sales@eaglexp.com",
        "address": "1/18 Gravel Pit Rd, Darra, QLD, Australia 4067",
        "description": "EagleXP is a trusted heavy machinery spare parts supplier and importer, specialising in OEM, genuine, aftermarket and rebuilt components for machinery used across the mining, earthmoving, civil construction, infrastructure and agricultural industries. We supply high-quality machinery parts, components and tools for most major equipment brands, supporting fleets operating throughout Australia, Papua New Guinea and the Pacific Islands. Our strength lies in our ability to source the right part, at the right price, and deliver it fast - even to the most remote locations."
    },
    
    "behavior_guidelines": [
        "Always use the user's preferred name when known",
        "Be proactive in learning about the user",
        "Save important user information for future reference",
        "Maintain context across multiple conversation threads",
        "IMPORTANT UI RULES: When tools capture or generate images (like browser screenshots), the platform natively embeds them into the chat UI. You MUST NOT attempt to output Markdown image links, JSON placeholders, or hallucinate URLs to display them. Simply acknowledge the action occurred."
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
    },
    
    "use_browser_agent": {
        "description": "Delegate web browsing and automation tasks to specialized browser agent",
        "when_to_use": [
            "When user asks to search the web",
            "When user wants to browse a specific website",
            "When user needs to extract information from web pages",
            "When user asks about current/real-time online information"
        ],
        "prompt_template": """For web browsing tasks (searching, navigating websites, extracting online information), use the use_browser_agent tool by providing a clear description of the browsing task. The browser agent will handle all web automation and return the results to you."""
    },

    "agent_awareness": {
        "description": "Inform the GeneralAgent about capabilities available via other agents",
        "when_to_use": [
            "When user asks about available capabilities",
            "When user asks about data or tools you don't directly have"
        ],
        "prompt_template": """You are part of a multi-agent system. While you handle general conversation, other specialized agents handle specific domains. When a user asks about capabilities you don't directly have, let them know what is available rather than saying "no".

Available specialist agents and their capabilities:
- **ProcurementAgent**: Has access to an internal product database, supplier database, brand database, and purchase history records. It can search for products by part number, brand, or description. It can find suppliers and their details. It can search purchase history and purchase orders to find which suppliers have supplied specific parts, pricing, quantities, and order dates. If you are asked about products, suppliers, purchase orders, or purchase history — let the user know you can help with that and the system will route their request appropriately.
- **BrowserAgent**: Can browse the web, search Google, navigate websites, and extract information from web pages.

IMPORTANT: Never say you don't have access to product data, supplier data, or purchase history. These capabilities exist in the system. If the user asks about them, confirm the capability exists and ask them to provide specifics so the request can be handled."""
    }
}

# =============================================================================
# PROFILE CONTEXT TEMPLATES
# =============================================================================
# Templates for formatting user profile information in system prompts.

PROFILE_TEMPLATES = {
    "header": "User profile information:",
    
    "sections": {
        "role": "- Role: {role}",
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
    
    # priority info like role
    if "role" in profile_data:
        sections.append(format_profile_section("role", profile_data["role"]))

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


def _build_action_awareness(profile_data: Optional[Dict[str, Any]] = None) -> str:
    """Build a prompt section listing available actions from the registry.

    Imports the action registry lazily to avoid circular imports.
    Filters actions based on the user's role in *profile_data*.
    """
    try:
        from includes.actions import get_actions_for_user, _registry
    except ImportError:
        return ""

    if not _registry:
        return ""

    # Determine a dummy user_id-like value for filtering.
    # The role is already resolved by GeneralAgent; we just need to know
    # whether to show admin-only items.
    is_admin = (profile_data or {}).get("role", "Staff") == "Admin"

    lines = [
        "You have access to the following action tools that the user can trigger via "
        "natural language or button clicks. When a user seems to be looking for "
        "available commands, features, or actions, use the list_available_actions tool "
        "or suggest the relevant action:",
        "",
    ]

    for action in _registry.values():
        if action.admin_only and not is_admin:
            continue
        admin_tag = " (admin only)" if action.admin_only else ""
        lines.append(f"- **{action.label}**{admin_tag}: {action.description}")

    return "\n".join(lines)


def _build_script_awareness(profile_data: Optional[Dict[str, Any]] = None) -> str:
    """Build a prompt section listing scripts an admin can run.

    Only included for Admin users. Imports the script registry lazily.
    """
    is_admin = (profile_data or {}).get("role", "Staff") == "Admin"
    if not is_admin:
        return ""

    try:
        from config.scripts import list_scripts
    except ImportError:
        return ""

    registry = list_scripts()
    if not registry:
        return ""

    lines = [
        "",
        "You have server-side script tools for running background tasks.",
        "",
        "Workflow:",
        "1. Use run_script to request a script run — this shows the user a confirmation button (Run/Cancel).",
        "2. After the user clicks Run, the job starts in the background. You will NOT receive the job ID directly.",
        "3. To check on a job, use list_jobs (shows all jobs with IDs and status) or get_job_status(script_name='...') to look it up by name.",
        "4. Use cancel_job to stop a running job.",
        "",
        "IMPORTANT: When asked about a job's status, ALWAYS call list_jobs or get_job_status — never say you can't check. You DO have these tools.",
        "",
        "Registered scripts:",
    ]

    for name, info in registry.items():
        lines.append(f"- **{name}**: {info['description']}")

    return "\n".join(lines)


def _build_admin_profile_hint(profile_data: Optional[Dict[str, Any]] = None) -> str:
    """Build a hint directing admin users to the System Admin profile for script tasks."""
    is_admin = (profile_data or {}).get("role", "Staff") == "Admin"
    if not is_admin:
        return ""

    return (
        "\nServer administration tasks such as running scripts, updating embeddings, "
        "importing data, and checking background jobs are available in the **System Admin** "
        "chat profile. If the user asks about these tasks, let them know they can switch to "
        "that profile using the dropdown at the top of the chat."
    )


def build_sysadmin_prompt(profile_data: Optional[Dict[str, Any]] = None) -> str:
    """
    Build the system prompt for the System Admin agent.

    Includes agent identity (in admin mode), user profile context,
    and the full script/job awareness section.
    """
    parts = []

    current_time = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).strftime("%A, %Y-%m-%d %H:%M:%S")
    parts.append(f"The current date and time in AEST (UTC+10) is: {current_time}.")
    parts.append("")

    parts.append(f"You are {AGENT_CONFIG['name']} in System Admin mode.")
    parts.append(
        "You help administrators manage server-side scripts and background jobs."
    )
    parts.append("You are professional, concise, and focused on operational tasks.")
    parts.append("")

    if profile_data:
        parts.append(PROFILE_TEMPLATES["header"])
        parts.extend(build_profile_context(profile_data))
        parts.append("")

    parts.append(_build_script_awareness(profile_data or {"role": "Admin"}))

    return "\n".join(parts).strip()


def build_system_prompt(
    profile_data: Optional[Dict[str, Any]] = None,
    available_tool_names: Optional[List[str]] = None
) -> str:
    """
    Build the complete system prompt for the agent.
    
    This function constructs the system message that provides context to the LLM,
    including agent identity, user profile information (if available),
    and tool usage instructions.
    
    Args:
        profile_data: Optional dictionary containing user profile information.
                     If None, only agent identity and tool instructions are included.
        available_tool_names: Optional list of tool names to include instructions for.
                             If None, includes all tool instructions.
                             Use this for dynamic/context-aware prompts.
    
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
        
        >>> # With specific tools only
        >>> prompt = build_system_prompt(None, ["remember_user_info"])
        >>> "remember_user_info" in prompt
        True
        >>> "use_browser_agent" not in prompt
        True
    """
    parts = []
    
    # Inject current date and time
    current_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=10))).strftime("%A, %Y-%m-%d %H:%M:%S")
    parts.append(f"The current date and time in AEST (UTC+10) is: {current_time}.")
    parts.append("Unless the user specifies a different time zone, present all times in AEST (UTC+10).")
    parts.append("")  # Blank line
    
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
    
    # Add tool instructions (either all or filtered by available_tool_names)
    if available_tool_names is None:
        # Include all tool instructions
        for tool_name, tool_config in TOOL_INSTRUCTIONS.items():
            parts.append(tool_config["prompt_template"])
            parts.append("")  # Spacing between instructions
    else:
        # Only include instructions for available tools
        for tool_name in available_tool_names:
            if tool_name in TOOL_INSTRUCTIONS:
                parts.append(TOOL_INSTRUCTIONS[tool_name]["prompt_template"])
                parts.append("")  # Spacing between instructions

    # Dynamic action awareness section built from the action registry
    parts.append(_build_action_awareness(profile_data))

    # Redirect admin users to System Admin profile for script/job tasks
    parts.append(_build_admin_profile_hint(profile_data))
    
    return "\n".join(parts).strip()


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
    
    if "company_info" in AGENT_CONFIG:
        info = AGENT_CONFIG["company_info"]
        parts.append("")
        parts.append(f"You represent a company called \"{info['name']}\".")
        parts.append(f"Website: {info['website']}")
        parts.append(f"Phone number: {info['phone']}")
        parts.append(f"Email: {info['email']}")
        parts.append(f"Head office address: {info['address']}")
        parts.append(f"Company description: {info['description']}")

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
