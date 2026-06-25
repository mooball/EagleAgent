"""
Procurement Agent for product and supplier searches.
"""

import asyncio
import re
from typing import List, Dict, Any, Optional
from langchain_core.messages import AIMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from .base import BaseSubAgent
from includes.tools.product_tools import search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history
from includes.tools.quote_tools import create_quote_tools
from includes.prompts import load_prompt

logger = logging.getLogger(__name__)

# Intent keywords that indicate the user explicitly requested RFQ work
_RFQ_INTENT_KEYWORDS = ("new_rfq", "RFQ", "Request for Quote", "manage_rfq")

# Keywords that trigger the programmatic "find suppliers" pipeline
_FIND_SUPPLIERS_KEYWORDS = (
    "find supplier", "find suppliers", "search supplier", "search suppliers",
    "get supplier", "get suppliers", "source supplier", "source suppliers",
    "supplier search", "look for supplier",
)


class ProcurementAgent(BaseSubAgent):
    """
    Specialized agent for searching products, parts, and suppliers.
    """
    
    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None, internal_only: bool = False):
        super().__init__("ProcurementAgent", model, store)
        self._rfq_active = False
        self._internal_only = internal_only
    
    async def __call__(self, state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
        # Determine if RFQ workflow should be active based on the thread's intent
        # or the user's current dashboard view (viewing an RFQ detail page)
        intent_context = state.get("intent_context") or ""
        self._rfq_active = any(kw in intent_context for kw in _RFQ_INTENT_KEYWORDS)
        if not self._rfq_active:
            # Check ALL messages for RFQ context (not just the last one,
            # since dashboard context may have been in an earlier turn)
            messages = state.get("messages", [])
            for msg in reversed(messages):
                content = msg.content if hasattr(msg, "content") else ""
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
                if "rfq_detail" in content or "RFQ-" in content:
                    self._rfq_active = True
                    break

        logger.info(f"ProcurementAgent: _rfq_active={self._rfq_active}, internal_only={self._internal_only}")

        # ---- Programmatic "find suppliers" pipeline ----
        # Guard: skip for internal-only mode.
        if self._rfq_active and not self._internal_only:
            messages = state.get("messages", [])

            # Check if this is a typed "yes" to the web search question (fallback for buttons)
            result = await self._try_web_search_typed_yes(state, messages)
            if result is not None:
                return result

            # Guard: don't re-run if pipeline output is already in messages
            # for THIS turn (check only AI messages after the last HumanMessage).
            from langchain_core.messages import HumanMessage as _HM
            last_human_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], _HM) or (hasattr(messages[i], "type") and messages[i].type == "human"):
                    last_human_idx = i
                    break

            pipeline_already_ran = False
            if last_human_idx is not None:
                for m in messages[last_human_idx + 1:]:
                    if hasattr(m, "type") and m.type == "ai" and hasattr(m, "content"):
                        c = m.content if isinstance(m.content, str) else str(m.content)
                        if "Step 1" in c:
                            pipeline_already_ran = True
                            break
            if not pipeline_already_ran:
                try:
                    result = await self._try_find_suppliers_pipeline(state)
                    if result is not None:
                        return result
                    logger.info("ProcurementAgent: pipeline returned None, falling through to ReAct")
                except Exception as e:
                    logger.error(f"ProcurementAgent: pipeline CRASHED: {type(e).__name__}: {e}", exc_info=True)
                    # Fall through to ReAct agent as fallback

        return await super().__call__(state, config)

    async def _try_find_suppliers_pipeline(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the find-suppliers workflow programmatically if conditions are met.

        Returns the state update dict if the pipeline ran, or None if conditions
        weren't met (falls through to normal ReAct agent).
        """
        from langchain_core.messages import HumanMessage as HM

        messages = state.get("messages", [])
        if not messages:
            return None

        # Find the most recent HumanMessage (not just messages[-1], which
        # may be an AIMessage after supervisor re-routing or replay).
        last_human = None
        for msg in reversed(messages):
            if isinstance(msg, HM) or (hasattr(msg, "type") and msg.type == "human"):
                last_human = msg
                break
        if not last_human:
            return None

        content = last_human.content if hasattr(last_human, "content") else ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        content_lower = content.lower()

        logger.info(
            f"Pipeline: last_human type={type(last_human).__name__}, "
            f"content type={type(last_human.content).__name__}, "
            f"content_lower preview={content_lower[:150]!r}"
        )

        if not any(kw in content_lower for kw in _FIND_SUPPLIERS_KEYWORDS):
            logger.info(f"Pipeline: keyword check failed. content={content_lower[:120]}")
            return None

        # Extract RFQ ID — try current message first, then scan earlier messages
        rfq_id = self._extract_rfq_id(content)
        if not rfq_id:
            # Scan previous messages for RFQ ID (dashboard context may be in earlier turn)
            for msg in reversed(messages):
                if msg is last_human:
                    continue
                msg_content = msg.content if hasattr(msg, "content") else ""
                if isinstance(msg_content, list):
                    msg_content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in msg_content
                    )
                rfq_id = self._extract_rfq_id(msg_content)
                if rfq_id:
                    break

        # Fallback: check the dashboard context store directly
        if not rfq_id:
            user_id = state.get("user_id", "")
            if user_id:
                from includes.dashboard.context import get_context
                ctx = get_context(user_id)
                if ctx and ctx.get("id", "").startswith("RFQ-"):
                    rfq_id = ctx["id"]
                    logger.info(f"Pipeline: got RFQ ID from dashboard context store: {rfq_id}")

        if not rfq_id:
            logger.warning(f"Pipeline: could not extract RFQ ID from any message or dashboard context. Last msg content type={type(last_msg.content).__name__}, preview={str(last_msg.content)[:200]}")
            return None

        user_id = state.get("user_id", "")
        logger.info(f"ProcurementAgent: running programmatic find-suppliers pipeline for {rfq_id}")

        from includes.tools.rfq_crud import (
            _classify_rfq_items_sync, _group_rfq_items_sync,
            _find_purchase_suppliers_sync, _get_rfq_dict_sync,
            _find_brand_suppliers_sync, _cross_apply_suppliers_sync,
            _sort_rfq_suppliers_sync,
        )
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working, _stream_to_user

        results = []

        async def _emit(text: str) -> None:
            """Append to results and stream to user immediately."""
            results.append(text)
            await _stream_to_user(text + "\n")

        # Step 1: Classify items
        logger.info(f"Pipeline[{rfq_id}]: Step 1 - Classify")
        await _notify_agent_working("Classifying items...")
        classify_result = await asyncio.to_thread(
            _classify_rfq_items_sync, rfq_id, user_id, True
        )
        if isinstance(classify_result, dict) and "error" in classify_result:
            return self._make_response(state, f"Error classifying items: {classify_result['error']}")

        classified = classify_result["classified"]
        db_matches = classify_result["db_matches"]
        to_validate = classify_result["to_validate"]

        total_classified = sum(len(v) for v in classified.values())
        await _emit(f"**Step 1 — Classification:** {total_classified} items classified.")
        if classified["specific"]:
            await _emit(f"- {len(classified['specific'])} specific (part number + description)")
        if classified["branded"]:
            await _emit(f"- {len(classified['branded'])} branded (brand + description)")
        if classified["generic"]:
            await _emit(f"- {len(classified['generic'])} generic (description only)")
        if db_matches:
            await _emit(f"- {len(db_matches)} found in product database")

        # Note items needing validation
        needs_validation = [
            i for i in to_validate
            if not any(i["line"] == m[0] for m in db_matches)
        ]
        if needs_validation:
            await _emit(
                f"- ⚠️ {len(needs_validation)} item(s) not in our database "
                f"(will need web validation)"
            )

        await _notify_rfq_updated()

        # Step 2: Validate items not found in product DB
        logger.info(f"Pipeline[{rfq_id}]: Step 2 - Validate ({len(needs_validation)} items)")
        if needs_validation:
            await _notify_agent_working("Validating items via web search...")
            from includes.tools.rfq_crud import _validate_items_sync
            validation_result = await asyncio.to_thread(
                _validate_items_sync, rfq_id, needs_validation, user_id
            )
            validated = validation_result.get("validated", [])
            if validated:
                await _emit(f"\n**Step 2 — Validation:** Checked {len(validated)} item(s) via web search.")
                for v in validated:
                    status_icon = "✅" if v.get("status") == "confirmed" else "🟠"
                    await _emit(f"- {status_icon} Line {v['line']}: {v.get('findings', '')}")
                    if v.get("correct_part_number") and v.get("status") == "discrepancy":
                        await _emit(f"  Correct part number: {v['correct_part_number']}")
                await _notify_rfq_updated()
            elif validation_result.get("error"):
                await _emit(f"\n**Step 2 — Validation:** ⚠️ Web validation failed ({validation_result['error'][:80]})")
        else:
            await _emit(f"\n**Step 2 — Validation:** All items found in product database. No web check needed.")

        # Step 3: Group items
        logger.info(f"Pipeline[{rfq_id}]: Step 3 - Group")
        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        groupable_items = [
            {
                "line": i["line"],
                "input_description": i.get("input_description", ""),
                "part_number": i.get("part_number", ""),
                "brand": i.get("brand", ""),
            }
            for i in rfq_dict.get("items", [])
            if i.get("match") in ("specific", "branded")
        ]

        if len(groupable_items) >= 2:
            await _notify_agent_working("Grouping items...")
            try:
                group_result = await asyncio.to_thread(
                    _group_rfq_items_sync, rfq_id, groupable_items, user_id
                )
            except Exception as e:
                logger.error(f"Pipeline group step failed: {e}")
                group_result = {"error": str(e)}
            if isinstance(group_result, dict) and "error" not in group_result:
                groups = group_result.get("groups", [])
                ungrouped = group_result.get("ungrouped", [])
                if groups:
                    await _emit(
                        f"\n**Step 3 — Grouping:** {len(groups)} sourcing group(s) identified."
                    )
                else:
                    await _emit(
                        f"\n**Step 3 — Grouping:** No natural groupings found "
                        f"({len(ungrouped)} items are in different categories)."
                    )
                await _notify_rfq_updated()
        else:
            await _emit(f"\n**Step 3 — Grouping:** Skipped (fewer than 2 specific or branded items).")

        # Step 4: Find previous suppliers
        logger.info(f"Pipeline[{rfq_id}]: Step 4 - Find previous suppliers")
        await _notify_agent_working("Searching purchase history...")
        supplier_result = await asyncio.to_thread(
            _find_purchase_suppliers_sync, rfq_id, user_id
        )
        if isinstance(supplier_result, dict) and "error" not in supplier_result:
            total_added = supplier_result["added"]
            by_line = supplier_result["by_line"]
            if total_added > 0:
                await _emit(
                    f"\n**Step 4 — Previous Suppliers:** Found {total_added} supplier(s) "
                    f"from purchase history. Added to RFQ:"
                )
                for line, names in sorted(by_line.items()):
                    await _emit(f"- Line {line}: {', '.join(names)}")
            else:
                await _emit(
                    f"\n**Step 4 — Previous Suppliers:** No previous suppliers found "
                    f"in our purchase history."
                )
            await _notify_rfq_updated()

        # Step 4b: Brand-linked suppliers
        logger.info(f"Pipeline[{rfq_id}]: Step 4b - Brand suppliers")
        await _notify_agent_working("Finding brand-linked suppliers...")
        brand_result = await asyncio.to_thread(
            _find_brand_suppliers_sync, rfq_id, user_id,
        )
        if isinstance(brand_result, dict) and "error" not in brand_result:
            brand_added = brand_result["added"]
            if brand_added > 0:
                await _emit(
                    f"\n**Step 4b — Brand-Linked Suppliers:** Added {brand_added} "
                    f"Tier A supplier(s) linked to item brands."
                )
                for line, names in sorted(brand_result["by_line"].items()):
                    await _emit(f"- Line {line}: {', '.join(names)}")
            else:
                await _emit(
                    f"\n**Step 4b — Brand-Linked Suppliers:** No additional "
                    f"brand-linked suppliers found."
                )
            await _notify_rfq_updated()

        # Step 4c: Cross-apply within groups
        logger.info(f"Pipeline[{rfq_id}]: Step 4c - Cross-apply")
        cross_result = await asyncio.to_thread(
            _cross_apply_suppliers_sync, rfq_id, user_id,
        )
        if isinstance(cross_result, dict) and "error" not in cross_result:
            cross_added = cross_result["added"]
            if cross_added > 0:
                await _emit(
                    f"\n**Step 4c — Cross-Apply:** Shared {cross_added} supplier(s) "
                    f"across grouped items."
                )
            else:
                await _emit(
                    f"\n**Step 4c — Cross-Apply:** No additional cross-application needed."
                )
            await _notify_rfq_updated()

        # Sort all suppliers on every line item
        await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
        await _notify_rfq_updated()

        # Send action buttons for web search decision
        import chainlit as cl
        validation_note = ""
        if needs_validation:
            validation_note = " and validate the unmatched items"

        question = f"Would you like me to search the web for additional suppliers{validation_note}?"

        web_search_action = cl.Action(
            name="rfq_pipeline_web_search",
            payload={"rfq_id": rfq_id, "user_id": user_id},
            label="🔍 Search Web",
            description=f"Search the web for new suppliers{validation_note}",
        )
        no_thanks_action = cl.Action(
            name="rfq_dismiss",
            payload={},
            label="No thanks",
        )
        await cl.Message(
            content=question,
            author="EagleAgent",
            actions=[web_search_action, no_thanks_action],
        ).send()

        # Include the question in results so the AIMessage ends with "?"
        # — the supervisor uses this to detect "agent asked a question → FINISH"
        results.append(f"\n{question}")

        return self._make_response(state, "\n".join(results))

    # ------------------------------------------------------------------
    # Typed "yes" fallback for web search (buttons are preferred)
    # ------------------------------------------------------------------
    _AFFIRMATIVE_KEYWORDS = (
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead",
        "please", "do it", "go for it", "absolutely", "definitely",
        "search the web", "search web",
    )

    async def _try_web_search_typed_yes(
        self, state: Dict[str, Any], messages: list
    ) -> Optional[Dict[str, Any]]:
        """If the user typed 'yes' to the web search question (instead of
        clicking the button), trigger the web search via the action callback.

        Returns the state update dict if it ran, or None if conditions weren't met.
        """
        from langchain_core.messages import HumanMessage as HM

        if len(messages) < 2:
            return None

        # Find the most recent HumanMessage and its index
        last_human = None
        last_human_idx = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, HM) or (hasattr(m, "type") and m.type == "human"):
                last_human = m
                last_human_idx = i
                break
        if not last_human or last_human_idx == 0:
            return None

        # Guard: if web search already ran AFTER this HumanMessage, don't re-run
        for msg in messages[last_human_idx + 1:]:
            c = msg.content if isinstance(msg.content, str) else str(msg.content)
            if "Web search complete" in c:
                return None

        user_text = last_human.content if isinstance(last_human.content, str) else ""
        if isinstance(last_human.content, list):
            user_text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in last_human.content
            )
        user_lower = user_text.lower()

        if not any(kw in user_lower for kw in self._AFFIRMATIVE_KEYWORDS):
            return None

        # Check that previous AI message was the pipeline's web search question
        prev_ai = messages[last_human_idx - 1]
        prev_content = prev_ai.content if isinstance(prev_ai.content, str) else str(prev_ai.content)
        if "search the web for additional suppliers" not in prev_content:
            return None

        # Extract RFQ ID
        rfq_id = self._extract_rfq_id(prev_content)
        if not rfq_id:
            user_id = state.get("user_id", "")
            if user_id:
                from includes.dashboard.context import get_context
                ctx = get_context(user_id)
                if ctx and ctx.get("id", "").startswith("RFQ-"):
                    rfq_id = ctx["id"]
        if not rfq_id:
            return None

        # Trigger the web search via the same code as the button callback
        logger.info(f"ProcurementAgent: typed 'yes' detected, running web search for {rfq_id}")
        import chainlit as cl

        # Simulate the action callback by calling it directly
        from includes.chat.rfq_actions import on_rfq_pipeline_web_search
        action = cl.Action(
            name="rfq_pipeline_web_search",
            payload={"rfq_id": rfq_id, "user_id": state.get("user_id", "")},
            label="Search Web",
        )
        await on_rfq_pipeline_web_search(action)

        return self._make_response(state, f"Web search completed for {rfq_id}.")

    def _extract_rfq_id(self, content: str) -> Optional[str]:
        """Extract RFQ ID from message content (dashboard context or text)."""
        # Match RFQ-YYYY-NNNN (4+ digits in sequence part)
        match = re.search(r"(RFQ-\d{4}-\d{4,})", content)
        return match.group(1) if match else None

    def _make_response(self, state: Dict[str, Any], text: str) -> Dict[str, Any]:
        """Build a state update with an AIMessage response."""
        return {"messages": [AIMessage(content=text)]}

    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Provide procurement tools including RFQ tools (always available
        so users can view/update existing RFQs).
        """
        tools = [search_products, search_brands, search_suppliers, part_purchase_history, search_purchase_history]
        if not self._internal_only:
            tools.extend(create_quote_tools(user_id))
        return tools
    
    def get_system_prompt(self) -> str:
        """
        Procurement-specific workflow instructions.
        """
        base_prompt = load_prompt("procurement_agent")

        if self._internal_only:
            return base_prompt + """\n\n**Internal Agent Profile Boundaries:**
You are running as the Internal Agent — a database-only assistant. You do NOT have access to:
- Web browsing or internet search of any kind
- Google Search grounding
- Research tools (no product research, no supply chain research)
- RFQ management tools (no creating, viewing, or editing RFQs)
- Data deletion tools
- Server administration or script-running tools

Your ONLY capabilities are searching the internal database for products, brands, suppliers, and purchase history. If the user asks about RFQs, tell them to switch to the **Eagle Agent** profile. If they ask for web research, suggest the **Research Agent** profile."""
        elif self._rfq_active:
            return base_prompt + "\n\n" + load_prompt("rfq_workflow")
        else:
            return base_prompt + """\n**RFQ Policy:**
You have access to RFQ tools (`get_rfq`, `manage_rfq`) and should use them when the user asks to view, update, or manage an existing RFQ.

**Internet Search Policy:**
- NEVER search the internet for suppliers or products unless explicitly asked by the user.
- Always exhaust the internal database and purchase history first.
- After presenting local results, ask the user before searching externally: "Would you like me to search the internet for additional suppliers?"

**However, do NOT create new RFQs** in this session. If the user wants to create a new RFQ, tell them to use the **RFQ section on the dashboard**. Never proactively create an RFQ just because the user mentioned a list of products or parts — only do product/supplier lookups unless an RFQ creation is explicitly requested."""
