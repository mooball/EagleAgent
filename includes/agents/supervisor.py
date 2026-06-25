from typing import Dict, Any, Literal, List, Annotated
import re
import logging
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class RouteDecision(BaseModel):
    next_agent: Literal["GeneralAgent", "ProcurementAgent", "ResearchAgent", "FINISH"] = Field(
        description="The agent to route the task to, or FINISH if complete."
    )

MAX_STEPS = 4  # Hard cap — prevent infinite agent loops

# Keywords that indicate a "find suppliers" request — must route to
# ProcurementAgent (which has the programmatic pipeline), NOT ResearchAgent.
_FIND_SUPPLIERS_KEYWORDS = (
    "find supplier", "find suppliers", "search supplier", "search suppliers",
    "get supplier", "get suppliers", "source supplier", "source suppliers",
    "supplier search", "look for supplier",
)


class Supervisor:
    """Supervisor node that routes requests between agents using LLM-based
    delegation. Runs after EVERY message (user or agent) to enable
    agent-to-agent handoff.
    """
    def __init__(self, model: ChatGoogleGenerativeAI):
        self.model = model

    async def __call__(self, state: Dict[str, Any], config=None) -> Dict[str, Any]:
        messages = state["messages"]
        if not messages:
            return {"next_agent": "GeneralAgent"}

        last_message = messages[-1]
        step_count = state.get("step_count", 0)

        # ---- Loop guard: too many agent invocations ----
        if step_count >= MAX_STEPS:
            logger.warning(f"Supervisor: step_count={step_count} >= {MAX_STEPS}, forcing FINISH")
            return {"next_agent": "FINISH", "step_count": 0}

        # ---- Determine message source (needed by intent routing below) ----
        is_user_message = (
            isinstance(last_message, HumanMessage)
            or (hasattr(last_message, "type") and last_message.type == "human")
        )

        # ---- Intent-based routing (dashboard buttons & pipeline) ----
        # Check the FIRST LINE of intent_context for routing keywords.
        # Short keywords from buttons (e.g. "search_brands") and pipeline
        # prefixes (e.g. "research_suppliers\n...") both match.
        # Long descriptive contexts (e.g. the find_supplier default) don't
        # match because their first line is a sentence, not a keyword.
        intent_context = state.get("intent_context")
        if intent_context:
            first_line = intent_context.split("\n")[0].strip()
            if len(first_line) < 50:
                if any(s in first_line for s in ("research_suppliers", "web_research")):
                    if not is_user_message:
                        # ResearchAgent already responded — done
                        logger.info("Supervisor: ResearchAgent completed, FINISH")
                        return {"next_agent": "FINISH", "step_count": 0}
                    logger.info("Supervisor intent: ResearchAgent")
                    return {"next_agent": "ResearchAgent", "step_count": step_count + 1}
                if any(s in first_line for s in ("search_products", "search_suppliers",
                       "search_brands", "part_purchase_history", "search_purchase_history")):
                    logger.info("Supervisor intent: ProcurementAgent")
                    return {"next_agent": "ProcurementAgent", "intent_context": "", "step_count": step_count + 1}

        # ---- Quick exit: if an agent asked the user a question, FINISH ----
        # This prevents the supervisor from routing to another agent when
        # the current agent is waiting for user input.
        if not is_user_message:
            content = last_message.content if hasattr(last_message, "content") else str(last_message)
            content_str = content if isinstance(content, str) else str(content)
            # Check if the response ends with a question to the user
            stripped = content_str.rstrip()
            if stripped.endswith("?"):
                logger.info("Supervisor: agent asked user a question → FINISH")
                return {"next_agent": "FINISH", "step_count": 0}
            # Check if a pipeline step completed (web search, classification, etc.)
            if "Web search complete" in content_str or "Step 5" in content_str:
                logger.info("Supervisor: pipeline web search completed → FINISH")
                return {"next_agent": "FINISH", "step_count": 0}
            # Pipeline Steps 1-4c completed — agent is waiting for user decision
            if "Step 4" in content_str and "Step 1" in content_str:
                logger.info("Supervisor: pipeline steps 1-4 completed → FINISH")
                return {"next_agent": "FINISH", "step_count": 0}

        # ---- Deterministic routing: "find suppliers" on an RFQ ----
        # The user asking to find/search suppliers while viewing an RFQ must
        # ALWAYS go to ProcurementAgent (which has the programmatic pipeline).
        # Without this, the LLM may route to ResearchAgent for web search.
        if is_user_message:
            # Get content of the latest HumanMessage
            human_content = last_message.content if hasattr(last_message, "content") else ""
            if isinstance(human_content, list):
                human_content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in human_content
                )
            human_lower = human_content.lower()
            has_find_kw = any(kw in human_lower for kw in _FIND_SUPPLIERS_KEYWORDS)
            has_rfq_ctx = "rfq_detail" in human_lower or bool(re.search(r"rfq-\d{4}-\d{4,}", human_lower))
            if has_find_kw and has_rfq_ctx:
                logger.info("Supervisor: deterministic route → ProcurementAgent (find suppliers + RFQ context)")
                return {"next_agent": "ProcurementAgent", "step_count": 0}

            # ---- Deterministic: user said "yes" to pipeline web search question ----
            _YES_KEYWORDS = ("yes", "yeah", "yep", "sure", "ok", "okay", "go ahead",
                             "please", "do it", "absolutely", "search the web", "search web")
            if any(kw in human_lower for kw in _YES_KEYWORDS):
                # Check if previous AI message was the pipeline's web search question
                for prev_msg in reversed(messages[:-1]):
                    if hasattr(prev_msg, "type") and prev_msg.type == "ai":
                        prev_text = prev_msg.content if isinstance(prev_msg.content, str) else str(prev_msg.content)
                        if "search the web for additional suppliers" in prev_text:
                            logger.info("Supervisor: deterministic route → ProcurementAgent (yes to web search)")
                            return {"next_agent": "ProcurementAgent", "step_count": 0}
                        break  # Only check the most recent AI message

        # ---- LLM-based routing for ALL messages ----
        content = last_message.content if hasattr(last_message, "content") else str(last_message)

        # Build system prompt with delegation context
        source_context = ""
        if is_user_message:
            source_context = "The latest message is FROM THE USER. They expect a response."
        else:
            source_context = (
                "The latest message is FROM AN AGENT. Decide if the agent's work "
                "is complete (FINISH) or if another agent needs to take over "
                "(e.g. ResearchAgent for web validation, ProcurementAgent to "
                "continue a workflow)."
            )

        system_prompt = f"""You are a supervisor managing a team of expert agents.
Route the conversation to the correct agent based on what needs to happen next.

Available agents:
- ProcurementAgent: Products, parts, brands, suppliers, purchase history, RFQs.
  Searches INTERNAL database. Handles RFQ workflows: classify, group, find
  suppliers, add suppliers. Use for ANY supplier/product/RFQ task including
  processing research results that need to be added to the RFQ.
- ResearchAgent: Web search via Google Search grounding. Use for:
  (a) validating part numbers/product details online
  (b) finding new suppliers via web (only when user has given permission)
  Reports findings but does NOT modify RFQs directly.
- GeneralAgent: General conversation, non-procurement topics, generic web info.
- FINISH: The conversation is complete. Use when an agent has provided a final
  answer and no further work is needed.

Routing rules:
- {source_context}
- If an agent response mentions needing web validation or checking part numbers
  online → route to ResearchAgent
- If the user says "yes" to searching the web for suppliers → ResearchAgent
- If ResearchAgent just returned results → route back to ProcurementAgent
  to continue the workflow (it will process and add results to the RFQ)
- If the user asks about suppliers, products, parts, RFQs → ProcurementAgent
- If the task is complete and no delegation is needed → FINISH
- Step count: {step_count}/{MAX_STEPS}. If near the limit, prefer FINISH.

Given the conversation, which agent should act next?
"""

        model_with_structured_output = self.model.with_structured_output(RouteDecision)
        recent_messages = messages[-8:]
        # Filter out messages with empty content — Gemini rejects requests
        # that contain messages without at least one non-empty parts field.
        filtered_messages = []
        for m in recent_messages:
            c = m.content if hasattr(m, "content") else None
            if c is None:
                continue
            if isinstance(c, str) and not c.strip():
                continue
            if isinstance(c, list) and not c:
                continue
            filtered_messages.append(m)
        eval_messages = [SystemMessage(content=system_prompt)] + filtered_messages

        logger.debug(f"Supervisor LLM routing (step={step_count})")
        try:
            merged_config = dict(config) if config else {}
            tags = merged_config.get("tags", [])
            if "supervisor_routing" not in tags:
                tags.append("supervisor_routing")
            merged_config["tags"] = tags

            decision = await model_with_structured_output.ainvoke(eval_messages, config=merged_config)
            logger.info(f"Supervisor: {decision.next_agent} (step={step_count})")

            # Reset step count on user messages, increment on agent messages
            new_step = 0 if is_user_message else step_count + 1
            return {"next_agent": decision.next_agent, "step_count": new_step}
        except Exception as e:
            logger.error(f"Supervisor LLM routing failed: {e}, defaulting to FINISH")
            return {"next_agent": "FINISH", "step_count": 0}
