# Prompt File Index

Each file in this directory is a "skill definition" loaded by the agent system. Below is a map of which agent loads each prompt, when, and for what purpose.

| File | Purpose | Loaded By | When |
|------|---------|-----------|------|
| `procurement_agent.md` | ProcurementAgent system prompt — search tools, RFQ policy, supplier finding workflow, clarification policy | `ProcurementAgent.get_system_prompt()` | Always (base prompt for Eagle Agent / Internal Agent) |
| `rfq_workflow.md` | RFQ mandatory checklist — 7-step workflow, gate rules, disambiguation guidance | `ProcurementAgent.get_system_prompt()` | Appended to base prompt when `_rfq_active=True` (user is viewing or working on an RFQ) |
| `rfq_identify_items.md` | Web-based discrepancy detection — validate part numbers against brand/description via web search | `rfq_actions._dispatch_discrepancy_check()` — sent as part of a synthetic message to ResearchAgent | When "Classify & Validate" button finds items needing web validation |
| `rfq_item_grouping.md` | Item grouping instructions — group specific items by brand/supply chain using LLM | `rfq_crud._group_rfq_items_sync()` — direct Gemini call | When 2+ specific items need grouping (pipeline step 3, or batch button) |
| `rfq_find_suppliers.md` | Web supplier discovery rules — geographic priority, supplier selection, adding to RFQ | ProcurementAgent / ResearchAgent | When user confirms they want web search for suppliers |
| `rfq_find_all_suppliers.md` | Legacy batch web search prompt — superseded by the unified pipeline. Still used by `_phase_new_suppliers()` for backward compat. | `rfq_actions._phase_new_suppliers()` | When "Find New Suppliers" batch button is clicked (standalone web search, not through pipeline) |
| `research_agent.md` | ResearchAgent system prompt — Google Search grounding, web research methodology | `ResearchAgent.get_system_prompt()` | Always (base prompt for Research Agent profile) |
| `product_research.md` | Product research instructions — detailed product lookup and verification | ResearchAgent | When product research is needed |
| `supply_chain_research.md` | Supply chain research — finding and verifying suppliers, distributors, OEMs | ResearchAgent | When supply chain research is needed |
| `browser_agent.md` | BrowserAgent system prompt — browser automation instructions | `BrowserAgent.get_system_prompt()` | When browser agent is invoked |
| `supplier_categorization.md` | Supplier categorization rules — tier assignment, supply chain position taxonomy | `scripts/categorize_suppliers.py` | During batch supplier categorization jobs |

## Prompt Loading Mechanism

Prompts are loaded via `includes/prompts.load_prompt(name)` which reads from `config/prompts/{name}.md`. The function caches prompts in memory after first load.

```python
from includes.prompts import load_prompt

# Returns the markdown content as a string
workflow_prompt = load_prompt("rfq_workflow")
```

## Agent Prompt Assembly

The agent system prompt is assembled dynamically:

1. **Base prompt** — `procurement_agent.md` (or `research_agent.md`, etc.)
2. **Conditional append** — e.g., `rfq_workflow.md` is appended when the agent detects RFQ context
3. **Action awareness** — `_build_action_awareness()` in `includes/prompts.py` dynamically lists actions the user can access (filtered by role)
4. **Boundary rules** — Added for Internal Agent profile to restrict capabilities
