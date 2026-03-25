from typing import Dict, Any, Literal, List, Annotated
import logging
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class RouteDecision(BaseModel):
    next_agent: Literal["GeneralAgent", "BrowserAgent", "ProcurementAgent", "FINISH"] = Field(
        description="The agent to route the task to, or FINISH if the user request has been fully answered."
    )

class Supervisor:
    """
    Supervisor node that routes requests to the appropriate sub-agent using hybrid routing.
    """
    def __init__(self, model: ChatGoogleGenerativeAI):
        self.model = model
    
    async def __call__(self, state: Dict[str, Any], config=None) -> Dict[str, Any]:
        """
        Route to the next agent based on the conversation state.
        """
        messages = state["messages"]
        if not messages:
            return {"next_agent": "GeneralAgent"}
            
        last_message = messages[-1]
        
        # If the last message is from an AI but not the supervisor doing a hand-off, 
        # it might mean the sub-agent just answered.
        if last_message.type == "ai":
            # Just route to FINISH. The user will reply next.
            return {"next_agent": "FINISH"}

        # It's a HumanMessage. Let's do hybrid routing.
        content = last_message.content if hasattr(last_message, "content") else str(last_message)
        if isinstance(content, list):
            # Handle multimodal/list content
            text_parts = []
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
                elif isinstance(part, str):
                    text_parts.append(part)
            user_text = " ".join(text_parts).lower()
        elif isinstance(content, str):
            user_text = content.lower()
        else:
            user_text = str(content).lower()
        
        # Rule-based fast routing (kept as escape hatch for unambiguous cases)
        # Empty for now — LLM routing handles all cases
        browser_keywords = []
        procurement_keywords = []
        
        if any(keyword in user_text for keyword in browser_keywords):
            logger.info("Supervisor rule-based routing: BrowserAgent")
            return {"next_agent": "BrowserAgent"}
            
        if any(keyword in user_text for keyword in procurement_keywords):
            logger.info("Supervisor rule-based routing: ProcurementAgent")
            return {"next_agent": "ProcurementAgent"}
            
        # LLM-based routing
        system_prompt = """You are a supervisor managing a team of expert agents.
Your job is to route the user's request to the correct agent.

Available agents:
- GeneralAgent: Use for general conversation, memory retrieval, task planning, document summarization. Also has built-in web search knowledge (Google Search grounding) so it can answer questions about products, companies, or topics using real-time web information — use it when the user wants external/public information about something, or wants to know more beyond what's in our internal database.
- BrowserAgent: Use ONLY for tasks that require interacting with a specific website — opening a URL, navigating pages, filling forms, clicking buttons, taking screenshots. Do NOT use for general information questions.
- ProcurementAgent: Use for searching the INTERNAL product catalog, finding part numbers, brands, product descriptions, supplier details, purchase history, purchase orders, and any questions about what products or suppliers exist in our database. Also use for questions like "how many products/suppliers/purchase orders do we have?" or "do you have purchase records?".
- FINISH: Use if the conversation is over or the request is fully fulfilled.

Routing guidelines:
- If the user asks about a product/supplier and wants INTERNAL database info → ProcurementAgent
- If the user wants MORE info, external details, web descriptions, or public knowledge about something → GeneralAgent
- If the user wants to navigate to a specific website or interact with a web page → BrowserAgent
- If unsure, prefer GeneralAgent

Given the conversation, which agent should act next?
"""
        
        model_with_structured_output = self.model.with_structured_output(RouteDecision)
        
        # Include recent conversation context so the supervisor can see what's
        # already been discussed (e.g., internal data already fetched)
        recent_messages = messages[-5:]  # Last few messages for context
        eval_messages = [SystemMessage(content=system_prompt)] + list(recent_messages)
        
        logger.debug("Supervisor LLM-based routing")
        try:
            # Provide tags so event stream can filter it out if needed, or simply let the default behavior work
            merged_config = dict(config) if config else {}
            tags = merged_config.get("tags", [])
            if "supervisor_routing" not in tags:
                tags.append("supervisor_routing")
            merged_config["tags"] = tags
            
            decision = await model_with_structured_output.ainvoke(eval_messages, config=merged_config)
            logger.info(f"Supervisor LLM chose: {decision.next_agent}")
            return {"next_agent": decision.next_agent}
        except Exception as e:
            logger.error(f"Supervisor LLM fallback failed, defaulting to GeneralAgent: {e}")
            return {"next_agent": "GeneralAgent"}
