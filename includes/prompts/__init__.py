"""
Centralized prompt templates and agent configuration for EagleAgent.

This package re-exports every public name so that existing
``from includes.prompts import X`` statements continue to work.

Sub-modules
-----------
config   – AGENT_CONFIG, TOOL_INSTRUCTIONS, PROFILE_TEMPLATES, RFQ_WORKFLOW_PROMPT
intents  – INTENTS, RESEARCH_INTENTS, get_intent_context()
builder  – build_system_prompt(), build_research_prompt(), build_sysadmin_prompt(), etc.
"""

# -- data -------------------------------------------------------------------
from includes.prompts.config import (  # noqa: F401
    AGENT_CONFIG,
    PROFILE_TEMPLATES,
    RFQ_WORKFLOW_PROMPT,
    TOOL_INSTRUCTIONS,
)
from includes.prompts.intents import (  # noqa: F401
    INTENTS,
    RESEARCH_INTENTS,
    get_intent_context,
)

# -- functions --------------------------------------------------------------
from includes.prompts.builder import (  # noqa: F401
    _build_action_awareness,
    _build_admin_profile_hint,
    _build_script_awareness,
    build_profile_context,
    build_research_prompt,
    build_sysadmin_prompt,
    build_system_prompt,
    format_profile_section,
    get_agent_identity_prompt,
    validate_config,
)
