from typing import List, Dict, Any
from langchain_core.tools import BaseTool
import logging

from includes.agents.base import BaseSubAgent

logger = logging.getLogger(__name__)

class CodeAgent(BaseSubAgent):
    """
    FUTURE IMPLEMENTATION: Specialized agent for code generation, debugging, and refactoring.
    """
    
    def __init__(self, model, store=None):
        super().__init__("CodeAgent", model, store)
        # TODO: Initialize code-specific tools here
        
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Provide code-specific tools (file operations, ast analysis, etc.)."""
        # TODO: Implement tools creation 
        return []
        
    def get_system_prompt(self) -> str:
        """Code-specific workflow instructions."""
        return """You are CodeAgent, specialized in software development.
        
        # TODO: Provide full code-writing instructions.
        """
