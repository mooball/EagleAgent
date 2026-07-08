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
from includes.tools.supplier_quote_pipeline import create_supplier_quote_tools
from includes.prompts import load_prompt

logger = logging.getLogger(__name__)

# Intent keywords that indicate the user explicitly requested RFQ work
_RFQ_INTENT_KEYWORDS = ("new_rfq", "RFQ", "Request for Quote", "manage_rfq")


class ProcurementAgent(BaseSubAgent):
    """
    Specialized agent for searching products, parts, and suppliers.
    """
    
    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None, internal_only: bool = False):
        super().__init__("ProcurementAgent", model, store)
        self._rfq_active = False
        self._internal_only = internal_only
        self._pending_gate: Optional[str] = None  # Set when RFQ has a pending pipeline gate
        self._pending_rfq_id: Optional[str] = None
    
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

        # ---- Check supervisor-classified intent ----
        intent = state.get("intent", "")
        self._current_intent = intent  # For get_system_prompt() to reference

        # ---- Run pipeline only when supervisor classified as RUN_WORKFLOW ----
        if intent == "RUN_WORKFLOW" and not self._internal_only:
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

        # ---- Pending gate awareness ----
        # For question/query contexts, inject reminder if a pipeline gate is waiting.
        self._pending_gate = None
        self._pending_rfq_id = None
        if intent in ("RFQ_QUERY", "DB_QUERY", "") and self._rfq_active:
            try:
                messages = state.get("messages", [])
                rfq_id = self._extract_rfq_id_from_messages(messages)
                if rfq_id:
                    from includes.tools.rfq_crud import _get_rfq_dict_sync
                    rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
                    if rfq_dict:
                        stage = rfq_dict.get("pipeline_stage", "unprocessed")
                        if stage in ("awaiting_web_search", "validation_gate"):
                            self._pending_gate = stage
                            self._pending_rfq_id = rfq_id
            except Exception:
                pass  # Non-critical

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

        # Intent is already classified by supervisor — only RUN_WORKFLOW reaches here.
        # Proceed directly to RFQ ID extraction and pipeline execution.
        intent = state.get("intent", "")
        logger.info(f"Pipeline: proceeding with supervisor intent={intent}")

        # Extract RFQ ID — try current message first, then scan earlier messages
        rfq_id = self._extract_rfq_id(content)
        if not rfq_id:
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
            logger.warning("Pipeline: could not extract RFQ ID from any message or dashboard context.")
            return None

        user_id = state.get("user_id", "")

        # Load the RFQ to check its current pipeline stage
        from includes.tools.rfq_crud import _get_rfq_dict_sync
        rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq_dict:
            return self._make_response(state, f"Could not load {rfq_id}.")

        current_stage = rfq_dict.get("pipeline_stage", "unprocessed")

        items = rfq_dict.get("items", [])
        unmatched_items = [i for i in items if i.get("match") == "unmatched"]
        all_unmatched = len(unmatched_items) == len(items) and len(items) > 0

        # If all items are unmatched, reset to unprocessed (user cleared/reset the RFQ)
        if all_unmatched and current_stage != "unprocessed":
            logger.info(f"Pipeline[{rfq_id}]: all items unmatched, resetting stage to 'unprocessed'")
            await self._set_pipeline_stage(rfq_id, "unprocessed")
            current_stage = "unprocessed"

        # If pipeline already complete and no new unmatched items, inform user
        if current_stage == "complete" and not unmatched_items:
            return None  # Fall through to ReAct — agent can answer questions about the RFQ

        if current_stage == "validation_gate" and not all_unmatched:
            return self._make_response(
                state,
                "I'm waiting for your decision on the validation issues found earlier. "
                "Please use the buttons above or tell me how you'd like to proceed."
            )

        if current_stage == "awaiting_web_search" and not all_unmatched:
            # Re-show the web search buttons
            return await self._show_web_search_gate(rfq_id, user_id, state)

        # Map pipeline_stage to the next stage to run
        _STAGE_RESUME_MAP = {
            "classified": "validate",
            "validated": "group",
            "grouped": "suppliers_internal",
            "suppliers_internal": "web_search_gate",
        }

        # Determine start stage for incremental vs full run
        if unmatched_items and current_stage in ("suppliers_internal", "awaiting_web_search", "complete"):
            # Incremental: process only new items from the start
            start_stage = "classify"
            items_filter = [i["line"] for i in unmatched_items]
            logger.info(f"Pipeline[{rfq_id}]: incremental run for lines {items_filter}")
        elif current_stage in _STAGE_RESUME_MAP:
            # Resume from where we left off (e.g., after failed validation)
            start_stage = _STAGE_RESUME_MAP[current_stage]
            items_filter = None
            logger.info(f"Pipeline[{rfq_id}]: resuming from stage '{start_stage}' (was '{current_stage}')")
        else:
            # Full run from beginning (or re-run)
            start_stage = "classify"
            items_filter = None

        logger.info(f"ProcurementAgent: running pipeline for {rfq_id} from stage '{start_stage}'")
        return await self._run_pipeline_from_stage(start_stage, rfq_id, user_id, state, items_filter)

    # ------------------------------------------------------------------
    # Stage-aware pipeline dispatcher
    # ------------------------------------------------------------------

    # Ordered list of pipeline stages
    _PIPELINE_STAGES = ("classify", "validate", "group", "suppliers_internal", "web_search_gate")

    async def _run_pipeline_from_stage(
        self,
        start_stage: str,
        rfq_id: str,
        user_id: str,
        state: Dict[str, Any],
        items_filter: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Run pipeline stages sequentially from start_stage.

        Stops and returns when a gate is hit or all stages complete.
        items_filter: if set, only process these line numbers (incremental).
        """
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working, _stream_to_user

        results: List[str] = []

        async def _emit(text: str) -> None:
            results.append(text)
            await _stream_to_user(text + "\n")

        # Context object passed through stages
        ctx = {
            "rfq_id": rfq_id,
            "user_id": user_id,
            "state": state,
            "items_filter": items_filter,
            "emit": _emit,
            "results": results,
            # Populated by stages:
            "needs_validation": [],
            "validated": [],
            "has_discrepancies": False,
        }

        start_idx = self._PIPELINE_STAGES.index(start_stage)
        for stage in self._PIPELINE_STAGES[start_idx:]:
            logger.info(f"Pipeline[{rfq_id}]: running stage '{stage}'")
            gate_hit = await self._run_stage(stage, ctx)
            if gate_hit:
                # Stage hit a gate — pipeline pauses
                return self._make_response(state, "\n".join(results))

        # All stages complete
        return self._make_response(state, "\n".join(results))

    async def _run_stage(self, stage: str, ctx: dict) -> bool:
        """Run a single pipeline stage. Returns True if a gate was hit (pipeline should pause)."""
        if stage == "classify":
            return await self._stage_classify(ctx)
        elif stage == "validate":
            return await self._stage_validate(ctx)
        elif stage == "group":
            return await self._stage_group(ctx)
        elif stage == "suppliers_internal":
            return await self._stage_suppliers_internal(ctx)
        elif stage == "web_search_gate":
            return await self._stage_web_search_gate(ctx)
        return False

    # ------------------------------------------------------------------
    # Individual pipeline stages
    # ------------------------------------------------------------------

    async def _stage_classify(self, ctx: dict) -> bool:
        """Step 1: Classify items."""
        from includes.tools.rfq_crud import _classify_rfq_items_sync
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        rfq_id, user_id, _emit = ctx["rfq_id"], ctx["user_id"], ctx["emit"]

        logger.info(f"Pipeline[{rfq_id}]: Step 1 - Classify")
        await _notify_agent_working("Classifying items...")
        classify_result = await asyncio.to_thread(
            _classify_rfq_items_sync, rfq_id, user_id, True
        )
        if isinstance(classify_result, dict) and "error" in classify_result:
            await _emit(f"Error classifying items: {classify_result['error']}")
            return True  # Stop on error

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

        # Determine items needing validation
        needs_validation = [
            i for i in to_validate
            if not any(i["line"] == m[0] for m in db_matches)
        ]
        if needs_validation:
            await _emit(
                f"- ⚠️ {len(needs_validation)} item(s) not in our database "
                f"(will need web validation)"
            )
        ctx["needs_validation"] = needs_validation

        await _notify_rfq_updated()
        await self._set_pipeline_stage(rfq_id, "classified")
        return False

    async def _stage_validate(self, ctx: dict) -> bool:
        """Step 2: Validate items not found in product DB. Gate if discrepancies found."""
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        rfq_id, user_id, _emit = ctx["rfq_id"], ctx["user_id"], ctx["emit"]
        needs_validation = ctx["needs_validation"]

        # If resuming (classify didn't run this session), reconstruct from DB
        if not needs_validation:
            from includes.tools.rfq_crud import _get_rfq_dict_sync
            rfq_dict = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
            needs_validation = [
                {
                    "line": i["line"],
                    "input_description": i.get("input_description", ""),
                    "part_number": i.get("part_number", ""),
                    "brand": i.get("brand", ""),
                }
                for i in rfq_dict.get("items", [])
                if i.get("match") in ("specific", "branded") and not i.get("product_id")
            ]
            ctx["needs_validation"] = needs_validation

        logger.info(f"Pipeline[{rfq_id}]: Step 2 - Validate ({len(needs_validation)} items)")
        if needs_validation:
            await _notify_agent_working("Validating items via web search...")
            from includes.tools.rfq_crud import _validate_items_sync
            validation_result = await asyncio.to_thread(
                _validate_items_sync, rfq_id, needs_validation, user_id
            )
            validated = validation_result.get("validated", [])
            ctx["validated"] = validated

            if validated:
                await _emit(f"\n**Step 2 — Validation:** Checked {len(validated)} item(s) via web search.")
                discrepancies = []
                for v in validated:
                    status_icon = "✅" if v.get("status") == "confirmed" else "🟠"
                    await _emit(f"- {status_icon} Line {v['line']}: {v.get('findings', '')}")
                    if v.get("correct_part_number") and v.get("status") == "discrepancy":
                        await _emit(f"  Correct part number: {v['correct_part_number']}")
                        discrepancies.append(v)
                await _notify_rfq_updated()

                # GATE: if discrepancies found, pause for user decision
                if discrepancies:
                    ctx["has_discrepancies"] = True
                    await self._set_pipeline_stage(rfq_id, "validation_gate")
                    await self._show_validation_gate(rfq_id, user_id, discrepancies, ctx)
                    return True  # Pipeline pauses

            elif validation_result.get("error"):
                await _emit(f"\n**Step 2 — Validation:** ⚠️ Web validation failed ({validation_result['error'][:80]})")
                # Show retry button
                import chainlit as cl
                actions = [
                    cl.Action(
                        name="rfq_pipeline_retry_validation",
                        payload={"rfq_id": rfq_id, "user_id": user_id},
                        label="🔄 Retry Validation",
                    ),
                    cl.Action(
                        name="rfq_pipeline_skip_validation",
                        payload={"rfq_id": rfq_id, "user_id": user_id},
                        label="⏭️ Skip Validation",
                    ),
                ]
                await cl.Message(
                    content="Would you like to retry validation or skip it?",
                    actions=actions,
                    author="EagleAgent",
                ).send()
                # Don't advance stage — keep at 'classified' so validation can be retried
                return True  # Pipeline pauses
        else:
            await _emit(f"\n**Step 2 — Validation:** All items found in product database. No web check needed.")

        await self._set_pipeline_stage(rfq_id, "validated")
        return False

    async def _stage_group(self, ctx: dict) -> bool:
        """Step 3: Group items."""
        from includes.tools.rfq_crud import _group_rfq_items_sync, _get_rfq_dict_sync
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        rfq_id, user_id, _emit = ctx["rfq_id"], ctx["user_id"], ctx["emit"]

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

        await self._set_pipeline_stage(rfq_id, "grouped")
        return False

    async def _stage_suppliers_internal(self, ctx: dict) -> bool:
        """Steps 4/4b/4c: Find internal suppliers."""
        from includes.tools.rfq_crud import (
            _find_purchase_suppliers_sync, _find_brand_suppliers_sync,
            _cross_apply_suppliers_sync, _sort_rfq_suppliers_sync,
        )
        from includes.tools.quote_tools import _notify_rfq_updated, _notify_agent_working

        rfq_id, user_id, _emit = ctx["rfq_id"], ctx["user_id"], ctx["emit"]

        # Step 4: Previous suppliers
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

        # Sort all suppliers
        await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
        await _notify_rfq_updated()

        await self._set_pipeline_stage(rfq_id, "suppliers_internal")
        return False

    async def _stage_web_search_gate(self, ctx: dict) -> bool:
        """Gate: Ask user whether to search the web for additional suppliers."""
        rfq_id, user_id = ctx["rfq_id"], ctx["user_id"]
        _emit = ctx["emit"]

        await self._set_pipeline_stage(rfq_id, "awaiting_web_search")
        await self._show_web_search_gate_inline(rfq_id, user_id, ctx)
        return True  # Always a gate — pipeline pauses

    # ------------------------------------------------------------------
    # Gate presentation helpers
    # ------------------------------------------------------------------

    async def _show_validation_gate(
        self, rfq_id: str, user_id: str, discrepancies: list, ctx: dict
    ) -> None:
        """Present validation discrepancy buttons to the user."""
        import chainlit as cl
        _emit = ctx["emit"]

        await _emit("\n---")
        await _emit("**⚠️ Validation issues found.** How would you like to proceed?")

        actions = []
        for d in discrepancies:
            correct_pn = d.get("correct_part_number", "")
            line = d["line"]
            if correct_pn:
                actions.append(cl.Action(
                    name="rfq_pipeline_fix_part",
                    payload={
                        "rfq_id": rfq_id,
                        "user_id": user_id,
                        "line": line,
                        "correct_part_number": correct_pn,
                        "total_discrepancies": len(discrepancies),
                    },
                    label=f"✏️ Fix Line {line} → {correct_pn}",
                ))

        actions.append(cl.Action(
            name="rfq_pipeline_skip_validation",
            payload={"rfq_id": rfq_id, "user_id": user_id},
            label="⏭️ Skip & Continue",
            description="Keep current part numbers and continue the pipeline",
        ))

        question = "Would you like me to fix the part numbers, or skip and continue?"
        await cl.Message(
            content=question,
            author="EagleAgent",
            actions=actions,
        ).send()
        ctx["results"].append(f"\n{question}")

    async def _show_web_search_gate_inline(self, rfq_id: str, user_id: str, ctx: dict) -> None:
        """Present web search buttons (inline — called from pipeline stage)."""
        import chainlit as cl
        _emit = ctx["emit"]
        needs_validation = ctx.get("needs_validation", [])

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
        ctx["results"].append(f"\n{question}")

    async def _show_web_search_gate(
        self, rfq_id: str, user_id: str, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Re-show web search gate (called when pipeline_stage is already awaiting_web_search)."""
        import chainlit as cl

        question = "Would you like me to search the web for additional suppliers?"
        web_search_action = cl.Action(
            name="rfq_pipeline_web_search",
            payload={"rfq_id": rfq_id, "user_id": user_id},
            label="🔍 Search Web",
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
        return self._make_response(state, question)

    # ------------------------------------------------------------------
    # Pipeline stage persistence helper
    # ------------------------------------------------------------------

    async def _set_pipeline_stage(self, rfq_id: str, stage: str) -> None:
        """Update the pipeline_stage field on the RFQ."""
        from includes.tools.rfq_crud import _get_session
        from includes.dashboard.models import RFQ as RFQModel

        def _update():
            session = _get_session()
            try:
                rfq = session.query(RFQModel).filter(RFQModel.rfq_number == rfq_id).first()
                if rfq:
                    rfq.pipeline_stage = stage
                    session.commit()
                    logger.info(f"Pipeline[{rfq_id}]: stage → '{stage}'")
            except Exception as e:
                logger.error(f"Failed to update pipeline_stage: {e}")
                session.rollback()
            finally:
                session.close()

        await asyncio.to_thread(_update)

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

    def _extract_rfq_id_from_messages(self, messages: list) -> Optional[str]:
        """Scan messages for an RFQ ID, falling back to dashboard context."""
        for msg in reversed(messages):
            content = msg.content if hasattr(msg, "content") else ""
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            rfq_id = self._extract_rfq_id(content)
            if rfq_id:
                return rfq_id
        return None

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
            tools.extend(create_supplier_quote_tools(user_id))
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
            # Only inject the full pipeline workflow when the user explicitly
            # asked to run it (RUN_WORKFLOW intent). For queries, just give
            # a lighter context about available tools.
            if getattr(self, "_current_intent", "") == "RUN_WORKFLOW":
                prompt = base_prompt + "\n\n" + load_prompt("rfq_workflow")
            else:
                prompt = base_prompt
            if self._pending_gate and self._pending_rfq_id:
                if self._pending_gate == "awaiting_web_search":
                    prompt += (
                        f"\n\n**PENDING DECISION:** The supplier pipeline for {self._pending_rfq_id} "
                        f"has completed internal supplier matching and is waiting for the user to decide "
                        f"whether to search the web for additional suppliers. After answering the user's "
                        f"current question, remind them: 'By the way, would you still like me to search "
                        f"the web for additional suppliers on {self._pending_rfq_id}? Just say **yes** "
                        f"or **find suppliers** to continue.'"
                    )
                elif self._pending_gate == "validation_gate":
                    prompt += (
                        f"\n\n**PENDING DECISION:** The supplier pipeline for {self._pending_rfq_id} "
                        f"found validation discrepancies and is waiting for the user's decision. "
                        f"After answering the user's current question, remind them about the "
                        f"pending validation issues and ask how they'd like to proceed."
                    )
            return prompt
        else:
            return base_prompt + """\n**RFQ Policy:**
You have access to RFQ tools (`get_rfq`, `manage_rfq`) and should use them when the user asks to view, update, or manage an existing RFQ.

**Internet Search Policy:**
- NEVER search the internet for suppliers or products unless explicitly asked by the user.
- Always exhaust the internal database and purchase history first.
- After presenting local results, ask the user before searching externally: "Would you like me to search the internet for additional suppliers?"

**However, do NOT create new RFQs** in this session. If the user wants to create a new RFQ, tell them to use the **RFQ section on the dashboard**. Never proactively create an RFQ just because the user mentioned a list of products or parts — only do product/supplier lookups unless an RFQ creation is explicitly requested."""
