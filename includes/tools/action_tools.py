"""
LangGraph tool wrappers for the action registry.

Exposes action-button functionality as tools the agent can invoke
via natural language (e.g. "start a new conversation").
"""

from langchain_core.tools import tool

from includes.actions import get_actions_for_user, dispatch_action


def create_action_tools(user_id: str):
    """Create action-related LangGraph tools bound to a user.

    Returns a list of tools:
      - list_available_actions  (all users)
      - start_new_conversation  (all users)
      - delete_all_user_data    (admin only — filtered out by GeneralAgent for non-admins)
    """

    @tool
    async def list_available_actions() -> str:
        """List the actions and commands available to the current user.

        Call this when the user asks what actions, commands, or features are
        available, e.g. "what actions can I perform?", "show me the menu",
        "what jobs/tasks/scripts/functions can I access?".

        Returns:
            A formatted list of available actions with descriptions.
        """
        actions = get_actions_for_user(user_id)
        if not actions:
            return "No actions are available."

        lines = []
        for a in actions:
            suffix = " (admin only)" if a.admin_only else ""
            lines.append(f"- **{a.label}**{suffix}: {a.description}")
        return "\n".join(lines)

    @tool
    async def start_new_conversation() -> str:
        """Start a fresh conversation thread, resetting the current chat context.

        Call this when the user says things like "start over", "new chat",
        "fresh conversation", "reset the conversation".

        Returns:
            Confirmation that a new thread was started.
        """
        await dispatch_action("new_conversation")
        return "A new conversation thread has been started."

    @tool
    async def delete_all_user_data() -> str:
        """Permanently delete all of the user's stored data including preferences,
        memories, and conversation history. This is an admin-only operation.

        Call this when the user explicitly asks to erase/delete all their data.
        A confirmation prompt with action buttons will be shown to the user.

        Returns:
            Confirmation that the deletion confirmation was sent.
        """
        await dispatch_action("delete_all_data")
        return "A confirmation prompt has been sent. The user must click a button to confirm or cancel."

    return [list_available_actions, start_new_conversation, delete_all_user_data]
