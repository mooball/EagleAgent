# Plan: RFQ Process Unification

## Overview
Unify the RFQ "Find Suppliers" workflow so that **dashboard buttons and chat messages invoke identical code paths**, the agent **always asks before web searching**, and the codebase is well-documented. The batch "Find All Suppliers" button will route through the ProcurementAgent pipeline via a synthetic message, eliminating duplicated logic and ensuring consistent behaviour regardless of how the user triggers the workflow.

### Current Problem
There are 5 overlapping find-supplier code paths that produce different outcomes:
1. `rfq_find_suppliers` (per-item button) — internal DB + web search **without asking**
2. `rfq_find_previous_suppliers` (batch button) — internal only
3. `rfq_find_new_suppliers` (batch button) — web only
4. `rfq_find_all_suppliers` (batch button) — runs both sequentially **without asking**
5. `_try_find_suppliers_pipeline` (chat message) — classify → validate → group → find_previous → **asks user** before web

The pipeline (path 5) is missing brand-linked supplier lookup and cross-apply within groups that the button path has. The button paths skip classification/validation and never ask before web search. This creates inconsistent UX — the same action via chat vs button produces different results.

### Target State
- **Batch button** routes through the same pipeline as chat — identical results
- **Pipeline** enhanced with brand lookup + cross-apply (matching the richness of the old button path)
- **Per-item button** stops after internal results and presents a "Search Web?" action button
- **Agent** asks for clarification on ambiguous requests rather than guessing
- **Batch buttons** disabled while agent is processing
- **Documentation** accurately reflects the actual system

### Design Pattern
The `_main_pinned()` pattern already exists in the codebase — `on_rfq_identify_items` uses it to dispatch discrepancy checks through the graph to ResearchAgent. The same pattern makes `rfq_find_all_suppliers` route through ProcurementAgent:

```
Button click → synthetic cl.Message("Find suppliers for all items on RFQ-2026-XXXX")
  → _main_pinned(synthetic, pinned_tid)
  → app.main() invokes graph
  → Supervisor routes to ProcurementAgent
  → _try_find_suppliers_pipeline picks up message (keyword match)
  → classify → validate → group → find_previous → brand → cross-apply → ASK
```

---

## Phase 1 — Foundation Functions ✅ COMPLETE

### 1. Add `_find_brand_suppliers_sync()` to `rfq_crud.py` ✅

Extract the Phase 2b brand-lookup logic currently inline in `_phase_previous_suppliers` (`rfq_actions.py` ~L750-830) into a reusable sync function in `includes/tools/rfq_crud.py`, placed after `_find_purchase_suppliers_sync()`.

**Function signature:**
```python
def _find_brand_suppliers_sync(rfq_number: str, user_id: str) -> dict:
    """Find brand-linked suppliers for all items with a brand, add top Tier A to RFQ.

    Looks up each item's brand in the supplier-brand link table via
    _find_brand_suppliers_with_tier(). Auto-adds up to 5 Tier A suppliers
    per line item. Stores the full brand-supplier list on the item's
    brand_suppliers JSON column for reference in the UI modal.

    Args:
        rfq_number: The RFQ identifier (e.g. "RFQ-2026-0042").
        user_id: Current user's identifier.

    Returns:
        {
            "added": int,                              # total Tier A suppliers added
            "by_line": {line_num: [supplier_names]},   # what was added where
        }
    """
```

**Implementation details:**
- Read items from DB via `_get_rfq_dict_sync(rfq_number)`
- For each item with a real brand (skip empty, "other", "n/a", "na", "none", "unknown"):
  - Call `_find_brand_suppliers_with_tier(brand)` from `includes/tools/product_tools.py`
  - Determine which suppliers are already on the line (from `item.suppliers`)
  - Filter to only new suppliers not already present
  - Auto-add top 5 Tier A suppliers via `_add_supplier_sync()` with `price_type="brand_link"`
  - Save full brand-supplier list to `item.brand_suppliers` JSON column (for modal reference)
- Return summary dict with `added` count and `by_line` mapping

**Add to exports in `includes/tools/quote_tools.py`:**
```python
from includes.tools.rfq_crud import (
    ...,
    _find_brand_suppliers_sync,
)
```

### 2. Add `_cross_apply_suppliers_sync()` to `rfq_crud.py` ✅

Extract the Phase 2.5 cross-apply logic currently inline in `_phase_previous_suppliers` (`rfq_actions.py` ~L835-890) into a reusable sync function in `includes/tools/rfq_crud.py`, placed after `_find_brand_suppliers_sync()`.

**Function signature:**
```python
def _cross_apply_suppliers_sync(rfq_number: str, user_id: str) -> dict:
    """Cross-apply suppliers within item groups so grouped items share suppliers.

    For each group (stored in rfq.item_groups), collects all suppliers found
    on any line in the group, then adds missing ones to peer lines. This
    ensures that if Line 1 has Supplier A and Line 2 has Supplier B, and
    both lines are in the same group, both lines end up with both suppliers.

    Uses direct JSON append on the RFQItem.suppliers column (bypasses
    enrichment) since these are cross-applied candidates, not new discoveries.

    Args:
        rfq_number: The RFQ identifier.
        user_id: Current user's identifier.

    Returns:
        {
            "added": int,    # total cross-applied supplier-line additions
            "details": [     # per-group breakdown
                {"group_label": str, "lines": [int], "suppliers_added": int}
            ]
        }
    """
```

**Implementation details:**
- Read RFQ from DB to get `item_groups` and current suppliers on each line
- For each group with 2+ lines:
  - Collect all unique suppliers across all lines in the group (keyed by name, case-insensitive)
  - For each line, determine which group suppliers it's missing
  - Append missing suppliers directly to the line's `suppliers` JSON (using `flag_modified`)
  - Supplier entries get `price_type="candidate"` and notes indicating cross-application
- Return summary dict

**Note:** There is already a `_cross_apply_suppliers_sync` helper function in `rfq_actions.py` (L95-130) that does the per-line append. The new function in `rfq_crud.py` wraps the group-iteration logic and can either inline the append logic or call a shared helper. Prefer inlining in `rfq_crud.py` to keep it self-contained.

---

## Phase 2 — Enhance Pipeline ✅ COMPLETE

### 3. Add Steps 4b + 4c to `_try_find_suppliers_pipeline` ✅

**File:** `includes/agents/procurement_agent.py`

In the `_try_find_suppliers_pipeline` method, add two new steps between the existing Step 4 (find_previous) and the "Final question" block.

**Add after the Step 4 block (~L260) and before `# Final question` (~L265):**

```python
# Step 4b: Brand-linked suppliers
logger.info(f"Pipeline[{rfq_id}]: Step 4b - Brand suppliers")
await _notify_agent_working("Finding brand-linked suppliers...")
from includes.tools.rfq_crud import _find_brand_suppliers_sync
brand_result = await asyncio.to_thread(_find_brand_suppliers_sync, rfq_id, user_id)
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
from includes.tools.rfq_crud import _cross_apply_suppliers_sync
cross_result = await asyncio.to_thread(_cross_apply_suppliers_sync, rfq_id, user_id)
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
from includes.tools.rfq_crud import _sort_rfq_suppliers_sync
await asyncio.to_thread(_sort_rfq_suppliers_sync, rfq_id)
await _notify_rfq_updated()
```

**Also add the new imports to the existing import block at the top of the method:**
```python
from includes.tools.rfq_crud import (
    _classify_rfq_items_sync, _group_rfq_items_sync,
    _find_purchase_suppliers_sync, _get_rfq_dict_sync,
    _find_brand_suppliers_sync, _cross_apply_suppliers_sync,
    _sort_rfq_suppliers_sync,
)
```

---

## Phase 3 — Route Batch Button Through Pipeline ✅ COMPLETE

### 4. Replace `on_rfq_find_all_suppliers` body with synthetic message ✅

### 5. Update per-item `on_rfq_find_suppliers` to stop-and-ask ✅

### 6. Update dashboard JS label ✅

---

## Phase 4 — Agent Clarification Behaviour ✅ COMPLETE

### 7. Add disambiguation rules to `rfq_workflow.md` ✅

### 8. Add clarification policy to `procurement_agent.md` ✅

---

## Phase 5 — UX Polish ✅ COMPLETE

### 9. Disable batch buttons while agent is working ✅

**`templates/base.html`** — Added `agentBusy: false` to `rfqDetail()` data, plus a
`window.addEventListener('message', ...)` in `init()` that sets `agentBusy = true`
on `agent_working` and `false` on `agent_done`.

**`templates/partials/rfq_detail.html`** — Added `:disabled="agentBusy"` and
`:class="{'opacity-50 cursor-not-allowed': agentBusy}"` to the "Find Previous
Suppliers" and "Find New Suppliers" batch buttons. Per-item buttons remain always
enabled (concurrent per-item use is valid).

---

## Phase 6 — Documentation ✅ COMPLETE

### 10. Rewrite `docs/RFQ_WORKFLOW.md` ✅

### 11. Create `config/prompts/README.md` ✅

**File:** `config/prompts/README.md`

Create a prompt index documenting each prompt file:

| File | Purpose | Loaded By | When |
|------|---------|-----------|------|
| `procurement_agent.md` | ProcurementAgent system prompt — search tools, RFQ policy, clarification rules | `ProcurementAgent.get_system_prompt()` | Always (base prompt) |
| `rfq_workflow.md` | RFQ mandatory checklist — 7-step workflow, gate rules, disambiguation | `ProcurementAgent.get_system_prompt()` | Appended when `_rfq_active=True` |
| `rfq_identify_items.md` | Web-based discrepancy detection — validate part numbers vs brand/description | `rfq_actions._dispatch_discrepancy_check()` | Dispatched to ResearchAgent via synthetic message |
| `rfq_item_grouping.md` | Item grouping instructions — group by brand/supply chain | `rfq_crud._group_rfq_items_sync()` | Direct LLM call (not via agent) |
| `rfq_find_suppliers.md` | Web supplier discovery rules — geographic priority, adding suppliers to RFQ | ProcurementAgent / ResearchAgent | When user confirms web search |

---

## Dependency Graph

```
Phase 1: Steps 1 + 2 (parallel — both add functions to rfq_crud.py)
    ↓
Phase 2: Step 3 (pipeline must have brand + cross-apply before button routes to it)
    ↓
Phase 3: Steps 4 + 5 + 6 (parallel — batch button, per-item button, JS label)
    ↓ (can also run in parallel with Phase 3)
Phase 4: Steps 7 + 8 (parallel — prompt updates)
    ↓ (can also run in parallel with Phase 3)
Phase 5: Step 9 (independent — dashboard JS)
    ↓
Phase 6: Steps 10 + 11 (documentation — run last after code changes settle)
```

## Test Plan

1. **Automated:** `uv run pytest tests/ -x --timeout=60` — all existing tests pass
2. **Manual — batch button:** Click "Find All Suppliers" on an RFQ with mixed unmatched/specific items → should see classify → validate → group → find_previous → brand → cross-apply → "Would you like me to search the web?" in chat
3. **Manual — chat message:** Type "Find suppliers for all items on RFQ-2026-XXXX" → identical outcome to test 2
4. **Manual — per-item button:** Click per-line "Find Suppliers" → internal results shown, then "Search Web?" action button appears in chat
5. **Manual — per-item web:** Click "Search Web?" button → web results added to that line
6. **Manual — button disable:** Click batch button while processing → batch buttons greyed out, per-item buttons still clickable
7. **Manual — disambiguation:** Send vague message like "find suppliers" without RFQ context → agent asks for clarification
8. **Documentation:** Verify `docs/RFQ_WORKFLOW.md` matches actual behaviour
