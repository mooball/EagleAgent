from typing import List, Dict, Any
from langchain_core.tools import BaseTool
import logging

from includes.agents.base import BaseSubAgent

logger = logging.getLogger(__name__)

class DataAgent(BaseSubAgent):
    """
    FUTURE IMPLEMENTATION: Specialized agent for data analysis and visualization.
    """
    
    def __init__(self, model, store=None):
        super().__init__("DataAgent", model, store)
        # TODO: Initialize data-specific tools here
        
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Provide data-specific tools (pandas manipulation, sql, plot generation)."""
        # TODO: Implement tools creation 
        return []
        
    def get_system_prompt(self) -> str:
        """Data-analysis workflow instructions."""
        return """You are DataAgent, specialized in analyzing datasets.
        
        # TODO: Provide full data analysis instructions.
        """
