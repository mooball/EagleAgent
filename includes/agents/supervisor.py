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
            # Partial pipeline run (gate hit, error, or pause) — don't re-route
            if "Step 1" in content_str and "Step 4" not in content_str:
                logger.info("Supervisor: partial pipeline run (gate/error) → FINISH")
                return {"next_agent": "FINISH", "step_count": 0}

            # ---- Default for AI messages: FINISH ----
            # After an agent responds, always stop unless there's an explicit
            # handoff signal. This prevents the LLM from re-routing based on
            # the agent's own output (e.g., "this mentions suppliers → 
            # route to ProcurementAgent again").
            # The only legitimate AI-to-AI handoff (ResearchAgent → 
            # ProcurementAgent) is handled via intent_context above.
            logger.info("Supervisor: AI message with no handoff signal → FINISH")
            return {"next_agent": "FINISH", "step_count": 0}

        # ---- User message: classify intent and route ----
        # Uses the intent classifier to determine what the user wants.
        # The direction list varies based on whether an RFQ is active.
        if is_user_message:
            from includes.intent_classifier import classify_intent
            from langchain_core.messages import AIMessage

            human_content = last_message.content if hasattr(last_message, "content") else ""
            if isinstance(human_content, list):
                human_content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in human_content
                )

            # Detect RFQ context — this determines which directions are available
            has_rfq_ctx = "rfq_detail" in human_content.lower() or bool(re.search(r"rfq-\d{4}-\d{4,}", human_content.lower()))

            # Get previous AI message for conversational context
            prev_ai_text = ""
            for msg in reversed(messages[:-1]):
                if hasattr(msg, "type") and msg.type == "ai" and hasattr(msg, "content"):
                    c = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if c.strip():
                        prev_ai_text = c.strip()[-300:]
                        break

            # Base directions available in all contexts
            base_directions = [
                {"id": "DB_QUERY", "description": "User wants information from the internal database — suppliers, products, brands, purchase history, transactions, or any existing records. They want to research or explore data, not search the web. Examples: 'find suppliers for the Wurth brand', 'who supplies Fluke products', 'show me purchase history for part 611343', 'list all brands we stock'"},
                {"id": "WEB_RESEARCH", "description": "User wants to search the web for information about a product, supplier, part number, or supply chain. They want external research. Examples: 'check if this part number is real', 'find australian suppliers for Fluke', 'research this company', 'is 611343X a valid part'"},
                {"id": "GENERAL", "description": "General conversation, greetings, or non-procurement topics. Examples: 'hello', 'what's the weather', 'tell me a joke', 'good morning'"},
            ]

            if has_rfq_ctx:
                # RFQ-specific directions — extended with base directions
                rfq_directions = [
                    {"id": "RUN_WORKFLOW", "description": "User wants to execute the supplier-finding pipeline on the RFQ's items. This means classify → validate → group → find suppliers. Examples: 'find suppliers for these items', 'source all of these', 'run the pipeline', 'match and validate', 'classify these parts', 'find me suppliers for this RFQ'"},
                    {"id": "RFQ_QUERY", "description": "User wants information about the current RFQ itself — its items, quantities, linked client, contacted suppliers, or status. They are asking about the RFQ's own data, not querying the general database. Examples: 'how many items in this RFQ?', 'which client is this RFQ linked to?', 'let me know which suppliers have been contacted', 'what's the total value', 'show me line 3 details'"},
                    {"id": "RFQ_UPDATE", "description": "User explicitly asks to modify RFQ data — add/remove suppliers, change quantities, update item details, or perform any CRUD operation. IMPORTANT: If the user's intent is ambiguous between a query and an update, do NOT assume update — classify as UNCERTAIN instead. Examples: 'add this supplier to line 2', 'remove all suppliers', 'change qty to 10', 'update line 4 part number'"},
                    {"id": "RFQ_ADD_ITEMS", "description": "User is providing a list of items (via pasted text or an uploaded image/file) to add to the current RFQ. They want the items extracted and added to the RFQ, then asked what to do next. They do NOT want research, web search, or the supplier-finding pipeline — just add the items to the RFQ. Examples: pasting a multi-line parts list, uploading a photo of a parts catalog page, or typing several item descriptions."},
                ]
                directions = rfq_directions + base_directions
                classifier_context = "User is viewing an RFQ in the procurement system."
            else:
                directions = base_directions
                classifier_context = "User is in the procurement system (not on a specific RFQ)."

            # Let the classifier know about file attachments — it can't see images
            # directly, and this context helps distinguish "image of parts to add"
            # from "image of a product to identify".
            file_attachments = state.get("file_attachments")
            if file_attachments:
                mime = file_attachments[0].get("mime_type", "") if file_attachments else ""
                file_type = "image" if mime.startswith("image/") else "document"
                classifier_context += (
                    f" The user also uploaded {len(file_attachments)} {file_type}(s)"
                    f" — this likely contains item data."
                )

            if prev_ai_text:
                classifier_context += f"\n\nThe assistant's previous message ended with: \"{prev_ai_text}\""

            intent = await classify_intent(
                message=human_content,
                directions=directions,
                context=classifier_context,
            )

            logger.info(f"Supervisor: intent={intent}, has_rfq_ctx={has_rfq_ctx}")

            # Route based on intent
            if intent == "RUN_WORKFLOW":
                return {"next_agent": "ProcurementAgent", "intent": "RUN_WORKFLOW", "step_count": 0}
            elif intent in ("RFQ_QUERY", "RFQ_UPDATE", "RFQ_ADD_ITEMS"):
                return {"next_agent": "ProcurementAgent", "intent": intent, "step_count": 0}
            elif intent == "DB_QUERY":
                return {"next_agent": "ProcurementAgent", "intent": "DB_QUERY", "step_count": 0}
            elif intent == "WEB_RESEARCH":
                return {"next_agent": "ResearchAgent", "step_count": 0}
            elif intent == "GENERAL":
                return {"next_agent": "GeneralAgent", "step_count": 0}
            else:
                # UNCERTAIN — ask the user to clarify
                clarification = "I'm not sure what you'd like me to do. Are you looking for information, looking to update something, or something else?"
                return {
                    "next_agent": "FINISH",
                    "messages": [AIMessage(content=clarification)],
                    "step_count": 0,
                }

        # ---- LLM-based routing for AI messages (fallback) ----
        # This path is only reached for AI messages that didn't match any
        # quick-exit condition above.
        content = last_message.content if hasattr(last_message, "content") else str(last_message)

        # Build system prompt with delegation context
        source_context = "The latest message is FROM AN AGENT. Decide if the agent's work is complete (FINISH) or if another agent needs to take over."

        system_prompt = f"""You are a supervisor managing a team of expert agents.
Route the conversation to the correct agent based on what needs to happen next.

Available agents:
- ProcurementAgent: Products, parts, brands, suppliers, purchase history, RFQs.
- ResearchAgent: Web search via Google Search grounding.
- GeneralAgent: General conversation, non-procurement topics.
- FINISH: The conversation is complete.

Routing rules:
- {source_context}
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
