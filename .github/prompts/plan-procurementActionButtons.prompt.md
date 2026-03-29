# EagleAgent Procurement Action Buttons

**Goal**: Add intent-based action buttons to the EagleAgent chat profile that prepopulate context so the LLM understands the user's procurement workflow without them needing to type a detailed prompt. Modelled after the existing action button pattern from `plan-actionButtons` but tailored for procurement use cases.

**Depends on**: `plan-actionButtons` (core action registry and dispatcher).

---

## Phase 1 — Intent Definitions & Action Registration ✅

### ~~1. Define intent constants in `includes/prompts.py`~~ ✅
- ~~Create an `INTENTS` dict mapping intent names to their metadata: label, icon, follow-up question, and LLM context string.~~
- ~~Five intents: `find_product_supplier`, `find_product`, `find_supplier`, `find_brand_supplier`, `check_purchase_history`.~~
- ~~Intent context strings tell the LLM which tools to use and how to approach the task.~~
- ~~Follow-up questions prompt the user for the details the agent needs.~~
- Added `INTENTS` dict and `get_intent_context()` helper function after `PROFILE_TEMPLATES`.

### ~~2. Register five new actions in `includes/actions.py`~~ ✅
- ~~Each action handler stores `intent_context` in `cl.user_session` and sends the follow-up question as an agent message.~~
- ~~Actions are **not** admin-only — all staff can use them.~~
- ~~Button definitions:~~
- Common `_handle_intent()` helper function sets session context and sends follow-up message with icon.

| Action Name | Label | Icon | Follow-up Question |
|---|---|---|---|
| `find_product_supplier` | Find a Product Supplier | 🏭 | "I understand you're looking to find a supplier for specific products. Can you tell me the product part numbers, name, or a detailed description?" |
| `find_product` | Find a Product | 📦 | "Sure — I can search our product database. Do you have a part number, brand name, supplier code, or a description of what you're looking for?" |
| `find_supplier` | Find a Supplier | 🔍 | "I can search our supplier database. Are you looking for a specific supplier by name, country, or do you have a description of what they should supply?" |
| `find_brand_supplier` | Find a Brand Supplier | 🏷️ | "I can find suppliers who carry a specific brand. What brand are you looking for?" |
| `check_purchase_history` | Check Purchase History | 📋 | "I can look up purchase history. Are you looking for a specific part number, supplier, PO number, or a date range?" |

### ~~3. Add `@cl.action_callback` handlers in `app.py`~~ ✅
- ~~One callback per action that calls `dispatch_action("action_name")`.~~
- ~~Follows the same pattern as existing `new_conversation` and `delete_all_data` callbacks.~~
- Five callbacks added after the `delete_all_data` callback.

---

## Phase 2 — Context Injection & Starters ✅

### ~~4. Inject intent context into the system prompt~~ ✅
- ~~In `app.py` `@cl.on_message`, check for `cl.user_session.get("intent_context")`.~~
- ~~If present, prepend it to the system prompt as a high-priority instruction.~~
- ~~The intent context **persists for the entire thread** — it is NOT cleared after use. The LLM always knows the user's original goal throughout the conversation.~~
- Added `intent_context` as `NotRequired[str]` field to `SupervisorState` TypedDict.
- `on_message` reads `cl.user_session.get("intent_context")` and passes it in the graph inputs.
- `BaseSubAgent.__call__` appends `**Current user intent:** {context}` to the system prompt when present.

### ~~5. Update `@cl.set_starters` with intent-aligned messages~~ ✅
- ~~Replace/extend the existing starters in `app.py` to align with the new intents.~~
- ~~Keep "What can you help me with?" as a general starter.~~
- ~~Add starters for each procurement intent that set the same session context.~~
- Starters updated: "What can you help me with?", "Find a product", "Find a supplier", "Check purchase history".

### ~~6. Show intent buttons on the EagleAgent welcome message~~ ✅
- ~~In `@cl.on_chat_start`, attach the five procurement action buttons to the EagleAgent welcome message (similar to how System Admin shows embedding update buttons).~~
- ~~Also show them on `@cl.on_chat_resume` for the EagleAgent profile.~~
- Intent buttons built dynamically from `INTENTS` dict with icon+label format.
- Added to both `on_chat_start` and `on_chat_resume` for the EagleAgent profile.

---

## Phase 3 — Prompt Awareness & Polish ✅

### ~~7. Update `_build_action_awareness()` to include new actions~~ ✅
- ~~The new actions are registered via `@register_action`, so they will automatically appear in `_build_action_awareness()`.~~
- ~~Verify the LLM prompt correctly lists all five new actions alongside the existing ones.~~
- Confirmed: all five new actions appear in action awareness prompt for staff users. Admin-only "Delete All My Data" is correctly filtered.

### ~~8. LLM context strings for each intent~~ ✅
- ~~**find_product_supplier**: "The user is specifically looking for a supplier from our current database who can fulfil a product order. Use `part_purchase_history` to find suppliers who have previously supplied the product, and `search_suppliers` to find additional matches. Prioritise suppliers with recent purchase history."~~
- ~~**find_product**: "The user wants to find a product in the internal product catalog. Use `search_products` with whatever identifiers they provide. If they give a vague description, use the semantic/vector search via the `description` parameter."~~
- ~~**find_supplier**: "The user wants to find a supplier in the internal supplier database. Use `search_suppliers` with whatever criteria they provide. The `query` parameter supports natural language and falls back to vector similarity search on supplier notes."~~
- ~~**find_brand_supplier**: "The user wants to find suppliers who carry a specific brand. First use `search_brands` to verify/resolve the brand name, then use `search_suppliers` with the `brand` parameter to find suppliers linked to that brand."~~
- ~~**check_purchase_history**: "The user wants to check past purchase history. Use `search_purchase_history` to find records matching their criteria. If they provide a specific part number, also use `part_purchase_history` to get a per-supplier summary. Dates use YYYY-MM-DD format."~~
- All context strings defined in `INTENTS` dict in `includes/prompts.py` and verified accessible via `get_intent_context()`.

### ~~9. Test the full flow~~ ✅
- ~~Verify buttons appear on the welcome message.~~
- ~~Verify buttons appear when user types "help" or "actions".~~
- ~~Verify clicking a button stores intent context and sends the follow-up question.~~
- ~~Verify the LLM receives the intent context on subsequent messages.~~
- ~~Verify intent context persists across the entire thread.~~
- All 95 related tests pass. Verified action registry, awareness prompt, and intent context retrieval programmatically.
