# Plan: Improve External Supplier Sourcing

## Overview

Refine how the Research Agent discovers and presents new (external) suppliers during web-based sourcing for RFQ line items. The current flow finds suppliers but does not reliably identify their **country**, **trading currency**, or **supply chain position** — and does not preference Australian suppliers over international ones. This plan addresses all four gaps.

---

## Current Architecture

The supplier sourcing flow has two phases, triggered by the "Find Suppliers" button on an RFQ line item (`app.py → on_rfq_find_suppliers`):

1. **Phase 1 (Internal):** Searches the local database for suppliers via purchase history. Adds any found directly to the RFQ with `supplier_id` and `purchase_ref`. This phase is working well and is not the focus of this plan.

2. **Phase 2 (External/Web):** Constructs a rich prompt and routes to the `ResearchAgent` (Gemini with Google Search grounding). The agent searches the web, gathers supplier details, then calls `manage_rfq(action='add_supplier')` to add them to the RFQ. **This is the phase we are improving.**

### Key files

| File | Role |
|------|------|
| `app.py` (lines ~1041–1195) | `on_rfq_find_suppliers` — orchestrates Phase 1 + Phase 2, builds the prompt for the Research Agent |
| `includes/agents/research_agent.py` | `ResearchAgent` class — Gemini with Google Search grounding |
| `includes/prompts.py` | `build_research_prompt()` — system prompt for the Research Agent; `RFQ_WORKFLOW_PROMPT` — shared RFQ instructions including the `add_supplier` contact requirements |
| `includes/tools/quote_tools.py` (~line 1037) | `manage_rfq(action='add_supplier')` — tool that actually writes suppliers to the RFQ |
| `includes/supplier_categorization.py` | Existing supplier categorization logic using search-grounded Gemini against the taxonomy |
| `config/supplier-categorization-taxonomy.md` | Supply chain taxonomy (OEM, Trade Wholesaler, Authorized Dealer, etc.) |

---

## Problem Statement

1. **Country not identified:** The Phase 2 prompt asks for `city`, `state`, `country` in contacts but this is treated as optional. The agent often skips it. Country is critical for procurement decisions.

2. **Currency not identified:** The agent sometimes displays estimated costs in AUD when the supplier actually trades in another currency (e.g. USD, GBP, EUR). There is no instruction to identify or record the supplier's trading currency.

3. **Supply chain position not classified:** The existing supplier categorization taxonomy (`config/supplier-categorization-taxonomy.md`) is only used in the batch categorization script. The Research Agent does not apply it when sourcing new suppliers, so new suppliers arrive with no tier or category.

4. **No geographic preference:** The agent searches globally with no bias toward Australian suppliers. For a procurement house based in Australia, local suppliers should be preferred (shorter lead times, no import duties, AUD pricing).

---

## Phase 1: Enhance the Research Agent Prompt for Country, Currency & Geo-Preference

**Goal:** Update the Phase 2 prompt (built in `app.py → on_rfq_find_suppliers`) and the shared RFQ workflow instructions (`includes/prompts.py`) so the Research Agent is explicitly instructed to:
- Always identify and record each supplier's country
- Identify the supplier's trading currency
- Preference Australian suppliers first, then expand internationally

### 1.1 Update the Phase 2 prompt in `app.py`

In the `on_rfq_find_suppliers` function, update the prompt construction (the `parts` list in Phase 2) to include clear instructions about geographic preference and required metadata:

**Replace** the current search instructions:
```
"Search the web for distributors and wholesalers who can supply this product."
"Prioritise authorised distributors and industrial wholesalers over retail sources."
"If distributors are scarce, include reputable retailers as fallback options."
"Aim for 3-5 good supplier options but more is fine if they look like strong matches."
```

**With** expanded instructions:
```
"Search the web for distributors and wholesalers who can supply this product."
""
"## Geographic Priority"
"1. FIRST search for Australian-based suppliers (add 'Australia' to your search queries)."
"2. If fewer than 3 Australian suppliers are found, expand to international suppliers."
"3. When listing results, present Australian suppliers first, then international."
""
"## Supplier Selection"
"Prioritise authorised distributors and industrial wholesalers over retail sources."
"If distributors are scarce, include reputable retailers as fallback options."
"Aim for 3-5 good supplier options but more is fine if they look like strong matches."
```

### 1.2 Update the contact/metadata requirements in `app.py` Phase 2 prompt

**Replace** the current supplier dict instruction:
```
"Each supplier dict must include: name, contacts (list with at least one of email/phone/url), and optionally price, price_type, lead_time."
```

**With:**
```
"Each supplier dict must include: name, country (2-letter ISO code, e.g. 'AU', 'US', 'GB'), currency (3-letter ISO code for their trading currency, e.g. 'AUD', 'USD', 'GBP'), contacts (list with at least one of email/phone/url)."
"Optional fields: price, price_type, lead_time, notes."
"If a price is in a foreign currency, store the ORIGINAL price and set currency accordingly — do NOT convert to AUD."
```

### 1.3 Update `RFQ_WORKFLOW_PROMPT` in `includes/prompts.py`

Update the "Finding suppliers for RFQ items" section to reinforce the same requirements. In particular:

- Add `country` and `currency` to the mandatory contact fields list
- Change the AUD conversion instruction: instead of converting foreign prices to AUD, store the original price with the correct currency code
- Add the geographic preference instruction (Australian suppliers first)

**Current text to update** (around line 169–180):
```
2. **MANDATORY — Contact details for EVERY supplier:** Before adding any supplier found via web search, you MUST gather their contact information. Do NOT add a supplier without at least a URL. For each supplier:
   - **url** (website) — REQUIRED. Every supplier must have a website URL. If you cannot find one, do not add the supplier.
   - **email** — include when available (check the supplier's contact/about page)
   - **phone** — include when available
   - **city**, **state**, **country** — include when available
   Pass these in the `contacts` list: `[{"url": "https://...", "email": "...", "phone": "...", "city": "...", "country": "..."}]`
   A supplier added without contacts is USELESS — the team cannot reach them. Never skip this step.
```

**Replace with:**
```
2. **MANDATORY — Contact details and metadata for EVERY supplier:** Before adding any supplier found via web search, you MUST gather their contact information and key metadata. Do NOT add a supplier without at least a URL. For each supplier:
   - **url** (website) — REQUIRED. Every supplier must have a website URL. If you cannot find one, do not add the supplier.
   - **email** — include when available (check the supplier's contact/about page)
   - **phone** — include when available
   - **city**, **state**, **country** — include when available (use 2-letter ISO country codes: AU, US, GB, DE, etc.)
   Pass these in the `contacts` list: `[{"url": "https://...", "email": "...", "phone": "...", "city": "...", "country": "AU"}]`
   A supplier added without contacts is USELESS — the team cannot reach them. Never skip this step.
   Additionally, each supplier dict (not just contacts) MUST include:
   - **country** — 2-letter ISO code (e.g. 'AU', 'US', 'GB'). REQUIRED.
   - **currency** — 3-letter ISO currency code for the supplier's trading currency (e.g. 'AUD', 'USD', 'GBP'). REQUIRED.
   **Geographic preference:** Always search for Australian suppliers first. Present AU-based suppliers before international ones. Only expand internationally if fewer than 3 Australian options are found.
   **Pricing currency:** If a supplier quotes prices in a foreign currency, store the original price with the correct currency — do NOT convert to AUD. Note the currency in the supplier's `notes` field if helpful.
```

### 1.4 Update the `manage_rfq` add_supplier handler in `quote_tools.py`

Update the `add_supplier` action handler to persist `country` and `currency` fields from the supplier dict into the RFQ data (they are currently ignored if passed). Ensure the supplier dict stored in the RFQ item's `suppliers` list preserves these fields.

Check the `_SUPPLIER_DEFAULTS` in `_normalize_rfq_suppliers()` in `routes.py` and add `country` and `currency` with `None` defaults so the template can safely reference them.

---

## Phase 2: Add Supply Chain Classification to External Sourcing

**Goal:** Have the Research Agent classify each new supplier's supply chain position (tier + category) using the existing taxonomy, so suppliers arrive pre-classified rather than needing a separate batch job.

### 2.1 Embed taxonomy summary in the Research Agent prompt

The full taxonomy in `config/supplier-categorization-taxonomy.md` is too long to include in every prompt. Create a condensed version for inline use.

Add a new function `get_taxonomy_summary()` to `includes/prompts.py` that returns a compact version of the classification decision logic:

```python
TAXONOMY_SUMMARY = """## Supply Chain Classification
Classify each supplier into ONE of these categories:
- **Tier A (Primary Sources):** OEM (brand owner/manufacturer), Aftermarket Manufacturer (makes equivalent parts)
- **Tier B (Industrial Trade):** Trade Wholesaler (B2B, login-for-price), Authorized Dealer (OEM contract), Machine Dismantler / Workshop / Parts (physical yard, used/recon parts)
- **Tier C (General Commercial):** Retail / Trade Outlet (physical store + trade desk), Online Distributor (e-commerce platform), Sourcing Broker (no stock, intermediary)
- **Tier D (Retail):** B2C Retailer (consumer-only), Hardware / Big Box (national chain)

**Quick classification logic:**
1. If they OWN the brand → OEM (always, even if they sell online)
2. If prices are publicly visible → Tier C or D (check if trade desk exists)
3. If "Login for Price" / "Request Quote" → Tier B (check if manufacturer or dealer)
4. If they make parts for other brands → Aftermarket Manufacturer
5. If they have a physical yard/workshop with used parts → Machine Dismantler
6. If they don't hold stock → Sourcing Broker
"""
```

### 2.2 Add taxonomy summary to the Phase 2 prompt in `app.py`

After the supplier selection instructions, append the taxonomy summary so the Research Agent can classify suppliers as it finds them:

```python
parts.append(TAXONOMY_SUMMARY)
parts.append("For each supplier you add, include 'tier' (A/B/C/D) and 'category' (e.g. 'Trade Wholesaler') in the supplier dict.")
```

### 2.3 Update `manage_rfq` add_supplier to persist tier/category

Ensure the `add_supplier` handler in `quote_tools.py` preserves `tier` and `category` fields in the supplier dict. Update `_SUPPLIER_DEFAULTS` to include these with `None` defaults.

### 2.4 Update `_normalize_rfq_suppliers` in `routes.py`

Add `tier`, `category`, `country`, and `currency` to the `_SUPPLIER_DEFAULTS` dict so existing RFQs don't break when templates reference these new fields.

### 2.5 Update the RFQ supplier display template

In `templates/partials/rfq_detail.html`, the tier badge is already displayed when `s.tier` exists (populated by DB enrichment). Since we're now setting tier at add-time for web-sourced suppliers, this should work automatically. Verify that the country display added previously also works for web-sourced suppliers (the `s.country` field).

---

## Phase 3: Persist Country/Currency When Saving to Database

**Goal:** When a web-sourced supplier is later matched or saved to the supplier database, carry the country and currency metadata forward.

### 3.1 Update `_enrich_rfq_supplier_contacts` in `routes.py`

This function already enriches RFQ suppliers with data from the DB (contacts, tier, terms, country). Ensure that if a web-sourced supplier already has `country` and `currency` set (from the Research Agent), these are NOT overwritten by null DB values. Only overwrite if the DB has a non-null value.

### 3.2 Future: Auto-create supplier records

This is out of scope for now but worth noting: when a web-sourced supplier is shortlisted or selected on an RFQ, a future enhancement could automatically create a `Supplier` record in the database with the country, currency, tier, and contacts already populated from the RFQ data.

---

## Testing

### Manual testing
1. Open an RFQ with a line item that has no internal suppliers (or create a new one)
2. Click "Find Suppliers" on the line item
3. Verify the Research Agent:
   - Searches for Australian suppliers first
   - Identifies country (2-letter code) for each supplier
   - Identifies currency for each supplier
   - Classifies each supplier's tier and category
   - Stores original foreign prices without converting to AUD
4. Verify the RFQ display shows:
   - Country code after supplier name (for non-AU suppliers)
   - Tier badge
   - Correct currency on any pricing

### Unit tests
- Update `tests/tools/test_quote_tools.py` to verify `add_supplier` preserves `country`, `currency`, `tier`, and `category` fields
- Update `tests/test_dashboard_routes.py` to verify `_normalize_rfq_suppliers` includes the new default fields

---

## Summary of Changes by File

| File | Changes |
|------|---------|
| `app.py` | Update Phase 2 prompt: geo-preference, country/currency requirements, taxonomy summary |
| `includes/prompts.py` | Update `RFQ_WORKFLOW_PROMPT`: mandatory country/currency, geo-preference, no AUD conversion. Add `TAXONOMY_SUMMARY` constant |
| `includes/tools/quote_tools.py` | Ensure `add_supplier` preserves `country`, `currency`, `tier`, `category` fields |
| `includes/dashboard/routes.py` | Add `country`, `currency`, `tier`, `category` to `_SUPPLIER_DEFAULTS` in `_normalize_rfq_suppliers`. Update `_enrich_rfq_supplier_contacts` to not overwrite web-sourced metadata with null DB values |
| `tests/tools/test_quote_tools.py` | Test new fields are preserved |
| `tests/test_dashboard_routes.py` | Test new defaults in normalization |
