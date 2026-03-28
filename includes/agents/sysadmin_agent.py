"""
System Administration agent for managing server-side scripts and background jobs.

This agent is used exclusively via the "System Admin" chat profile, providing
admin users with a focused interface for running scripts, monitoring jobs, and
performing system maintenance tasks.
"""

from typing import List, Optional, Dict, Any
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from includes.agents.base import BaseSubAgent
from includes.tools.user_profile import create_profile_tools
from includes.tools.job_tools import create_job_tools
from includes.prompts import build_sysadmin_prompt

logger = logging.getLogger(__name__)


class SysAdminAgent(BaseSubAgent):
    """
    System administration agent with job management tools.

    Available exclusively through the "System Admin" chat profile for admin users.
    Provides tools for running server-side scripts, monitoring background jobs,
    and performing system maintenance.
    """

    def __init__(
        self,
        model: ChatGoogleGenerativeAI,
        store: Optional[BaseStore] = None,
        job_runner=None,
    ):
        super().__init__("SysAdminAgent", model, store)
        self.job_runner = job_runner

    def get_tools(self, user_id: str) -> List[BaseTool]:
        tools = []
        if user_id and self.store:
            tools.extend(create_profile_tools(self.store, user_id))
        return tools

    async def get_tools_async(self, user_id: str) -> List[BaseTool]:
        tools = self.get_tools(user_id)
        if self.job_runner:
            tools.extend(create_job_tools(self.job_runner))
        return tools

    def get_system_prompt(self) -> str:
        return build_sysadmin_prompt({})

    async def get_system_prompt_async(self, user_id: str) -> str:
        user_profile = None
        if user_id and self.store:
            user_profile = await self.store.aget(("users",), user_id)
        profile_data = dict(user_profile.value) if (user_profile and user_profile.value) else {}
        profile_data["role"] = "Admin"
        return build_sysadmin_prompt(profile_data)
