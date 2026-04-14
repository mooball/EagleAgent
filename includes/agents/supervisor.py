from typing import Dict, Any, Literal, List, Annotated
import logging
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class RouteDecision(BaseModel):
    next_agent: Literal["GeneralAgent", "ProcurementAgent", "FINISH"] = Field(
        description="The agent to route the task to, or FINISH if the user request has been fully answered."
    )

# Extended decision type that includes ResearchAgent (used by intent routing only)
class ExtendedRouteDecision(BaseModel):
    next_agent: Literal["GeneralAgent", "ProcurementAgent", "ResearchAgent", "FINISH"] = Field(
        description="The agent to route the task to."
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

        # Intent-based routing: if an action button set a specific intent, honour it
        intent_context = state.get("intent_context")
        if intent_context:
            # Research intent signals → ResearchAgent (web search)
            research_intent_signals = [
                "research_suppliers", "web_research",
            ]
            if any(signal in intent_context for signal in research_intent_signals):
                logger.info("Supervisor intent-based routing: ResearchAgent (intent_context set)")
                return {"next_agent": "ResearchAgent"}

            # Procurement intent signals → ProcurementAgent (internal DB)
            procurement_intent_signals = [
                "search_products", "search_suppliers", "search_brands",
                "part_purchase_history", "search_purchase_history",
            ]
            if any(signal in intent_context for signal in procurement_intent_signals):
                logger.info("Supervisor intent-based routing: ProcurementAgent (intent_context set)")
                return {"next_agent": "ProcurementAgent"}

        # It's a HumanMessage. Route via LLM.
        content = last_message.content if hasattr(last_message, "content") else str(last_message)
            
        # LLM-based routing
        system_prompt = """You are a supervisor managing a team of expert agents.
Your job is to route the user's request to the correct agent.

Available agents:
- GeneralAgent: General conversation, memory retrieval, task planning, document summarization. Has Google Search grounding for answering questions using real-time web information. Use when the user explicitly wants external/public/web information, or for non-procurement topics.
- ProcurementAgent: Use for ANY question about products, parts, brands, suppliers, purchase history, or RFQs that should be answered from our INTERNAL database. This includes: finding suppliers for a product or brand, looking up part numbers, checking purchase orders, searching the product catalog, and asking about what we have in stock or on record. Also handles ALL RFQ management: loading, creating, updating, listing RFQs. When the user asks "who can supply X?" or "find a supplier for X" without specifying "search the web", default to ProcurementAgent.
- FINISH: Use ONLY after an agent has just responded and there is no new user question pending. NEVER choose FINISH when the latest message is from the user — the user is always expecting a response.

Routing guidelines:
- The latest message is from the user, so you MUST route to an agent. Do NOT choose FINISH.
- Questions about suppliers, products, brands, parts, purchase history, records, quotes → ProcurementAgent (unless the user explicitly asks for web/external info)
- "Search the web for..." or "find me information online about..." → GeneralAgent
- RFQ requests (load, create, update, show, list) → ProcurementAgent
- If the user wants MORE info beyond what our database returned, or explicitly asks for external/public knowledge → GeneralAgent
- If unsure between ProcurementAgent and GeneralAgent, prefer ProcurementAgent for supplier/product questions
- If unsure which agent to use, default to GeneralAgent. Never choose FINISH for a user question.

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
