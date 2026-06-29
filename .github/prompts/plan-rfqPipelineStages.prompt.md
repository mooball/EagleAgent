# Plan: RFQ Pipeline Stages & Decision Gates

## Objective

Refactor the "find suppliers" pipeline from a one-shot linear flow into a **stage-aware, resumable workflow** with human decision gates. The pipeline should:

1. Track progress via a `pipeline_stage` field on the RFQ.
2. Pause at decision points (validation discrepancies, web search approval) and present action buttons.
3. Resume from the correct stage when the user responds (button click or typed message).
4. Handle incremental changes (new items added, items edited) without re-running the entire pipeline.
5. Expose stage information to dashboard buttons so they can be shown/hidden/disabled appropriately.

---

## Current Architecture

### Pipeline flow (one-shot, no pausing)

```
_try_find_suppliers_pipeline():
    Step 1: Classify items
    Step 2: Validate (web search grounding)
    Step 3: Group items
    Step 4: Find previous suppliers (purchase history)
    Step 4b: Brand-linked suppliers
    Step 4c: Cross-apply within groups
    → Sort & emit buttons: [Search Web] [No thanks]
    → Return AIMessage (supervisor FINISHes)
```

### Problems with current approach

| Problem | Impact |
|---------|--------|
| Validation discrepancy found but pipeline continues | User never gets to decide what to do with bad part numbers |
| Pipeline is all-or-nothing | Adding one item requires full re-run |
| Dashboard buttons don't know pipeline state | "Find Previous Suppliers" button is always visible even if already done |
| No persistence of progress | If user closes browser mid-pipeline, progress is lost |
| "Re-run" not possible | No way to reset and start fresh |

### Key files

| File | Role |
|------|------|
| `includes/agents/procurement_agent.py` | `_try_find_suppliers_pipeline()` — the monolithic pipeline |
| `includes/chat/rfq_actions.py` | Action button callbacks (web search, dismiss) |
| `includes/tools/rfq_crud.py` | `_classify_rfq_items_sync`, `_validate_items_sync`, `_group_rfq_items_sync`, `_find_purchase_suppliers_sync`, `_find_brand_suppliers_sync`, `_cross_apply_suppliers_sync`, `_web_search_suppliers_sync` |
| `includes/dashboard/models.py` | `RFQ` model, `RFQItem` model |
| `templates/partials/rfq_detail.html` | Dashboard buttons, item table |
| `includes/agents/supervisor.py` | Deterministic routing for "find suppliers" + pipeline completion detection |

---

## Proposed Design

### 1. Pipeline Stage Field on RFQ

Add `pipeline_stage` column to the `RFQ` model:

```python
class RFQ(Base):
    ...
    pipeline_stage = Column(String, default="unprocessed")
```

Stage values (ordered progression):

| Stage | Meaning |
|-------|---------|
| `unprocessed` | No pipeline has run (new RFQ or reset) |
| `classified` | Items classified (Step 1 complete) |
| `validation_gate` | Validation found discrepancies — waiting for user decision |
| `validated` | Validation passed or user resolved discrepancies |
| `grouped` | Items grouped (Step 3 complete) |
| `suppliers_internal` | Internal suppliers found (Steps 4/4b/4c complete) |
| `awaiting_web_search` | Waiting for user to approve web search |
| `complete` | Web search done (or declined), pipeline finished |

### 2. Decision Gates

Only two gates initially (extensible later):

#### Gate 1: Validation Discrepancy

**Triggers when:** `_validate_items_sync` returns items with `status == "discrepancy"`

**Behaviour:**
- Update RFQ items: set `match = "discrepancy"`, store findings in `notes`
- Set `pipeline_stage = "validation_gate"`
- Emit validation results to user
- Present buttons per discrepancy:
  - `[✏️ Update to {correct_pn}]` — fixes the part number, re-validates
  - `[⏭️ Skip & Continue]` — leaves item as-is, continues pipeline
  - `[🛑 Stop Here]` — pauses pipeline, user can return later
- If no discrepancies: skip gate, set stage to `validated`, continue

**Resume:** Button callback sets stage to `validated` and calls `_run_pipeline_from_stage("grouped", rfq_id, user_id)`

#### Gate 2: Web Search Approval (already built)

**Triggers when:** Internal supplier search is complete

**Behaviour:**
- Set `pipeline_stage = "awaiting_web_search"`
- Present buttons: `[🔍 Search Web]` `[No thanks]`

**Resume:** Button callback runs web search, sets stage to `complete`

### 3. Resumable Pipeline Dispatcher

Replace the monolithic `_try_find_suppliers_pipeline` with a stage-aware dispatcher:

```python
async def _run_pipeline_from_stage(
    self, start_stage: str, rfq_id: str, user_id: str,
    items_filter: list[int] | None = None,  # Process only these lines (incremental)
) -> Optional[Dict[str, Any]]:
    """Run the pipeline from the given stage onward.
    
    Stops and returns when a gate is reached or the pipeline completes.
    items_filter: if set, only process these line numbers (for incremental runs).
    """
    stages = ["classify", "validate", "group", "suppliers_internal", "web_search_gate"]
    start_idx = stages.index(start_stage)
    
    for stage in stages[start_idx:]:
        result = await self._run_stage(stage, rfq_id, user_id, items_filter)
        if result.get("gate_hit"):
            return result  # Pipeline paused at a gate
    
    return result  # Pipeline completed
```

### 4. Incremental Processing (Item Changes)

When items are added/edited/removed after the pipeline has run:

| Action | Effect |
|--------|--------|
| Add new item | New item gets `match = "unmatched"`. RFQ `pipeline_stage` stays the same. |
| Edit item (part number/brand changed) | Reset item to `match = "unmatched"`. |
| Remove item | Just delete. No stage change. |
| User says "find suppliers" | Pipeline detects unmatched items and processes only those through classify → validate → find suppliers. Existing items' suppliers are untouched. |
| User clicks "Re-run Pipeline" | Reset ALL items to `match = "unmatched"`, set `pipeline_stage = "unprocessed"`. Full re-run. |

The `items_filter` parameter on the dispatcher enables this:

```python
unmatched_items = [i for i in rfq.items if i.match == "unmatched"]
if unmatched_items and rfq.pipeline_stage in ("suppliers_internal", "complete"):
    # Incremental: process only new items
    await self._run_pipeline_from_stage("classify", rfq_id, user_id, 
                                         items_filter=[i.line for i in unmatched_items])
else:
    # Full run from current stage
    await self._run_pipeline_from_stage(rfq.pipeline_stage, rfq_id, user_id)
```

### 5. Dashboard Button Visibility

In `rfq_detail.html`, buttons are shown/hidden based on `rfq.pipeline_stage`:

```html
<!-- Always visible: triggers full pipeline -->
{% if rfq.pipeline_stage == 'unprocessed' %}
<button hx-post="..." class="btn-primary">🔍 Find Suppliers</button>
{% endif %}

<!-- Visible after internal search, before web search -->
{% if rfq.pipeline_stage == 'awaiting_web_search' %}
<button hx-post="..." class="btn-primary">🌐 Find New Suppliers</button>
{% endif %}

<!-- Visible when complete -->
{% if rfq.pipeline_stage == 'complete' %}
<button hx-post="..." class="btn-primary">📋 Shortlist Suppliers</button>
{% endif %}

<!-- Always visible when stage > unprocessed -->
{% if rfq.pipeline_stage != 'unprocessed' %}
<button hx-post="..." class="btn-secondary">🔄 Re-run Pipeline</button>
{% endif %}
```

### 6. Agent Integration

The ProcurementAgent's `_try_find_suppliers_pipeline` becomes:

```python
async def _try_find_suppliers_pipeline(self, state):
    ...
    rfq = get_rfq(rfq_id)
    
    # Detect incremental vs full run
    unmatched = [i for i in rfq.items if i.match == "unmatched"]
    
    if rfq.pipeline_stage == "complete" and not unmatched:
        # Pipeline already done, nothing new
        return None  # Fall through to ReAct (agent can answer questions about the RFQ)
    
    if rfq.pipeline_stage == "validation_gate":
        # Waiting for user decision — don't re-run, just remind them
        return self._make_response(state, "I'm waiting for your decision on the validation issues above.")
    
    if rfq.pipeline_stage == "awaiting_web_search":
        # Re-show buttons
        return self._show_web_search_buttons(rfq_id, user_id)
    
    if unmatched and rfq.pipeline_stage in ("suppliers_internal", "complete"):
        # Incremental: process only new items
        start_stage = "classify"
        items_filter = [i.line for i in unmatched]
    else:
        # Full run from current stage (or from start)
        start_stage = rfq.pipeline_stage if rfq.pipeline_stage != "unprocessed" else "classify"
        items_filter = None
    
    return await self._run_pipeline_from_stage(start_stage, rfq_id, user_id, items_filter)
```

---

## Implementation Phases

### Phase 1: Foundation (minimal viable)
- [ ] Add `pipeline_stage` column to RFQ model (alembic migration)
- [ ] Refactor `_try_find_suppliers_pipeline` into `_run_pipeline_from_stage` dispatcher
- [ ] Update stage after each step completes
- [ ] Read stage at start to resume from correct point

### Phase 2: Validation Gate
- [ ] After validation, if discrepancies found: set stage to `validation_gate`, emit buttons
- [ ] Add action callbacks: `rfq_pipeline_fix_part`, `rfq_pipeline_skip_validation`
- [ ] Callbacks set stage to `validated` and resume pipeline

### Phase 3: Dashboard Integration
- [ ] Expose `pipeline_stage` to `rfq_detail.html` template
- [ ] Conditionally show/hide/disable buttons based on stage
- [ ] Add "Re-run Pipeline" button (resets stage + items)
- [ ] Add "Find Suppliers" button only for `unprocessed` state

### Phase 4: Incremental Processing
- [ ] Detect unmatched items when pipeline_stage is already advanced
- [ ] Run pipeline for only those items (pass `items_filter`)
- [ ] On item add/edit via `manage_rfq`, reset that item's `match` to `unmatched`
- [ ] Agent detects incremental scenario and runs accordingly

### Phase 5: Polish
- [ ] "Re-run Pipeline" confirmation dialog
- [ ] Progress indicator on dashboard showing current stage
- [ ] Handle edge cases: concurrent edits, pipeline timeout, etc.

---

## Migration

```python
# alembic revision
def upgrade():
    op.add_column('rfqs', sa.Column('pipeline_stage', sa.String(), server_default='unprocessed'))

def downgrade():
    op.drop_column('rfqs', 'pipeline_stage')
```

---

## Testing Strategy

- Unit tests for stage transitions (each step updates stage correctly)
- Test gate detection (discrepancies → pauses, no discrepancies → continues)
- Test resume from each stage (button callback starts from correct point)
- Test incremental processing (add item after complete → only processes new item)
- Test re-run (reset → full pipeline fires again)
- Integration test: full flow with gates and resumption

---

## Open Questions

1. Should `pipeline_stage` be per-RFQ or per-user-per-RFQ? (If two users work the same RFQ simultaneously)
2. Should we store per-item `stage` alongside the RFQ-level stage for fine-grained tracking?
3. Should the "validation gate" show ALL discrepancies at once with a batch decision, or one at a time?
4. When the user types "find suppliers" and the pipeline is at a gate, should we remind them of the pending decision or re-run from scratch?
