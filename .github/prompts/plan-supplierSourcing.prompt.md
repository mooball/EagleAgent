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

5. **False-positive supplier matching:** When the Research Agent finds a new supplier via web search, `_match_suppliers_to_db()` in `quote_tools.py` tries to match it against existing DB suppliers using `match_supplier_by_name()` from `database.py`. This function uses containment checks and trigram similarity (threshold 0.6), which is too loose — it can match "ABC Industrial" to "ABC Industrial Supplies" even when they are completely different companies in different countries. The match should verify additional attributes (domain name, country) before considering a supplier to be the same entity. This check should happen **early** in the flow (before classification) to avoid wasting time categorising a supplier that already exists.

---

## Phase 1: Stricter Supplier Deduplication During Web Sourcing ✅ DONE

**Goal:** Prevent false-positive matches when the system tries to link a web-sourced supplier to an existing DB record. Currently `_match_suppliers_to_db()` only checks name similarity. Add domain and country verification so a match is only accepted when corroborating attributes agree.

### Key files

| File | Role |
|------|------|
| `includes/dashboard/database.py` | `match_supplier_by_name()` — the two-pass fuzzy name matcher |
| `includes/tools/quote_tools.py` (~line 145) | `_match_suppliers_to_db()` — calls `match_supplier_by_name` and enriches supplier dicts with `supplier_id` |

### 1.1 Add a domain-extraction utility ✅

Add a helper function `_extract_domain(url: str) -> str | None` to `includes/dashboard/database.py` that extracts the root domain from a URL (e.g. `https://www.abcparts.com.au/contact` → `abcparts.com.au`). Use `urllib.parse.urlparse` — no external dependencies needed.

### 1.2 Create a stricter matching function ✅

Add a new function `match_supplier(name: str, url: str | None = None, country: str | None = None, session=None) -> Supplier | None` to `includes/dashboard/database.py` that:

1. Calls the existing `match_supplier_by_name()` to get a candidate.
2. If no candidate, return `None`.
3. If a candidate is found, **verify** it against available corroborating attributes:
   - **Domain check:** If the web-sourced supplier has a `url` and the DB candidate has contacts with a `url`, extract domains from both. If domains don't match, **reject** the match (return `None`).
   - **Country check:** If the web-sourced supplier has a `country` and the DB candidate has a `country`, and they differ, **reject** the match.
   - If neither domain nor country can be compared (both sides lack the data), fall back to accepting the name match only if the similarity is very high (exact containment match, not just trigram).
4. Return the verified candidate or `None`.

### 1.3 Update `_match_suppliers_to_db()` in `quote_tools.py` ✅

Replace the call to `match_supplier_by_name()` with the new `match_supplier()`, passing the supplier's URL (from contacts) and country code. This ensures false positives are caught before `supplier_id` is assigned and before any enrichment/classification happens.

```python
# Extract url and country from the supplier dict for verification
sup_url = None
for c in sup_list[0].get("contacts", []):
    if isinstance(c, dict) and c.get("url"):
        sup_url = c["url"]
        break
sup_country = sup_list[0].get("country")

row = match_supplier(name_lower, url=sup_url, country=sup_country, session=session)
```

### 1.4 Add logging for rejected matches ✅

When a name match is found but rejected due to domain/country mismatch, log it clearly so we can monitor the deduplication quality:

```python
logger.info(f"[supplier-match] '{name_lower}' name-matched '{row.name}' but REJECTED: domain/country mismatch")
```

---

## Phase 2: Enhance the Research Agent Prompt for Country, Currency & Geo-Preference ✅ DONE

**Goal:** Update the Phase 2 prompt (built in `app.py → on_rfq_find_suppliers`) and the shared RFQ workflow instructions (`includes/prompts.py`) so the Research Agent is explicitly instructed to:
- Always identify and record each supplier's country
- Identify the supplier's trading currency
- Preference Australian suppliers first, then expand internationally

### 2.1 Update the Phase 2 prompt in `app.py` ✅

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

### 2.2 Update the contact/metadata requirements in `app.py` Phase 2 prompt ✅

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

### 2.3 Update `RFQ_WORKFLOW_PROMPT` in `includes/prompts.py` ✅

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

### 2.4 Update the `manage_rfq` add_supplier handler in `quote_tools.py`

Update the `add_supplier` action handler to persist `country` and `currency` fields from the supplier dict into the RFQ data (they are currently ignored if passed). Ensure the supplier dict stored in the RFQ item's `suppliers` list preserves these fields.

Check the `_SUPPLIER_DEFAULTS` in `_normalize_rfq_suppliers()` in `routes.py` and add `country` and `currency` with `None` defaults so the template can safely reference them.

---

## Phase 3: Add Supply Chain Classification to External Sourcing ✅ DONE

> **Implementation note:** Phase 3 was superseded. Instead of embedding a condensed taxonomy in the agent prompt, the system now runs the **full categorization pipeline** (`includes/supplier_categorization.py`) server-side when a new web supplier is created. This uses the complete taxonomy with search-grounded Gemini, confidence scoring, and validation — identical to the batch categorization script. The agent prompt was simplified to say "the system will auto-classify" and tier/category are optional from the agent.

### 3.1 Embed taxonomy summary in the Research Agent prompt ✅ (superseded — auto-categorization instead)

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

### 3.2 Add taxonomy summary to the Phase 2 prompt in `app.py` ✅ (simplified — no longer needed)

After the supplier selection instructions, append the taxonomy summary so the Research Agent can classify suppliers as it finds them:

```python
parts.append(TAXONOMY_SUMMARY)
parts.append("For each supplier you add, include 'tier' (A/B/C/D) and 'category' (e.g. 'Trade Wholesaler') in the supplier dict.")
```

### 2.4 Update `manage_rfq` add_supplier to persist tier/category ✅ ✅

Ensure the `add_supplier` handler in `quote_tools.py` preserves `tier` and `category` fields in the supplier dict. Update `_SUPPLIER_DEFAULTS` to include these with `None` defaults.

### 3.4 Update `_normalize_rfq_suppliers` in `routes.py` ✅

Add `tier`, `category`, `country`, and `currency` to the `_SUPPLIER_DEFAULTS` dict so existing RFQs don't break when templates reference these new fields.

### 3.5 Update the RFQ supplier display template ✅

In `templates/partials/rfq_detail.html`, the tier badge is already displayed when `s.tier` exists (populated by DB enrichment). Since we're now setting tier at add-time for web-sourced suppliers, this should work automatically. Verify that the country display added previously also works for web-sourced suppliers (the `s.country` field).

---

## Phase 4: Persist Web-Sourced Suppliers to the Database ✅ DONE

**Goal:** When the Research Agent discovers a new supplier that does not match any existing DB record, immediately create a Supplier record in the database. This avoids re-classification on subsequent encounters, shares knowledge across RFQs, and survives RFQ deletion.

### 4.1 Add `source` column to Supplier model ✅

Add a `source` column to the `Supplier` model in `includes/dashboard/models.py`:

```python
source = Column(String(20), nullable=True, default='netsuite')  # 'netsuite' | 'web' | 'manual'
```

Create an Alembic migration to add the column and backfill existing records:
```sql
ALTER TABLE suppliers ADD COLUMN source VARCHAR(20) DEFAULT 'netsuite';
UPDATE suppliers SET source = 'netsuite' WHERE source IS NULL;
```

### 4.2 Create supplier records for new web-sourced suppliers ✅

Update `_match_suppliers_to_db()` in `quote_tools.py`. After the stricter `match_supplier()` call (Phase 1), if **no match** is found:

1. Create a new `Supplier` record with:
   - `name` from the supplier dict
   - `country` from the supplier dict
   - `currency` from the supplier dict
   - `url` extracted from contacts
   - `contacts` from the supplier dict
   - `supply_chain_position` = `{"tier": tier, "category": category}` if provided
   - `source` = `'web'`
   - `netsuite_id` = `None`
2. Assign the new `supplier_id` to the RFQ supplier dict
3. Log the creation: `[supplier-create] Created new web supplier 'ABC Parts' (id=...)`

This means the second time the same supplier is found (different line, different RFQ), `match_supplier()` will find the existing record — no re-search or re-classification needed.

### 4.3 Update `_enrich_rfq_supplier_contacts` in `routes.py` ✅

This function already enriches RFQ suppliers with data from the DB (contacts, tier, terms, country). Ensure that if a web-sourced supplier already has `country` and `currency` set (from the Research Agent), these are NOT overwritten by null DB values. Only overwrite if the DB has a non-null value.

### 4.4 Update the RFQ supplier database icon ✅

> **Implementation note:** The icon meaning was subsequently changed. The DB icon now means "has transaction history" (blue only, no amber). A green **NEW** badge shows for suppliers with no transaction history. The amber/web vs blue/netsuite distinction was removed.

In `templates/partials/rfq_detail.html`, the three-rings database icon (SVG) is currently shown in blue (`text-blue-500`) when `s.supplier_id` is set, indicating the supplier exists in the database. Update this to differentiate between NetSuite-synced and web-discovered suppliers:

- **Blue** (`text-blue-500`) — NetSuite-synced supplier (`source='netsuite'`), tooltip: "In database (NetSuite)"
- **Amber/orange** (`text-amber-500`) — Web-discovered supplier (`source='web'`), tooltip: "In database (web sourced)"

This requires passing the `source` field through to the template. Update `_enrich_rfq_supplier_contacts` to also fetch `Supplier.source` and set `sup["source"]` on each matched supplier dict. Then update both the popover-button and plain-text template branches:

```html
<!-- Popover button branch -->
{% if s.supplier_id %}
<svg class="w-3 h-3 {{ 'text-amber-500 dark:text-amber-400' if s.source == 'web' else 'text-blue-500 dark:text-blue-400' }} shrink-0 no-underline"
     fill="none" stroke="currentColor" viewBox="0 0 24 24"
     title="{{ 'In database (web sourced)' if s.source == 'web' else 'In database (NetSuite)' }}">
  <!-- existing three-rings path -->
</svg>
{% endif %}
```

Add `source` to `_SUPPLIER_DEFAULTS` with default `None` so the template can safely check `s.source`.

### 4.5 Handle NetSuite sync dedup ✅

When the NetSuite supplier sync (`scripts/sync_netsuite_suppliers.py`) creates new supplier records from NetSuite data, it should check for existing `source='web'` suppliers that match by name+domain before creating a new record. If a match is found, **merge** by updating the existing web record with the `netsuite_id` and changing `source` to `'netsuite'`. This prevents duplicates when a web-discovered supplier is later added to NetSuite independently.

Add this check to `sync_netsuite_suppliers.py` in the "new supplier" code path:
1. Before `INSERT`, query for `Supplier` records where `source='web'` and name matches (using `match_supplier_by_name` or a direct name comparison)
2. If found, also verify domain if both have URLs
3. If match confirmed: `UPDATE` the existing record with `netsuite_id`, `source='netsuite'`, and any additional NetSuite fields
4. If no match: `INSERT` as normal with `source='netsuite'`

---

## Phase 5: Persist Country/Currency from DB to RFQ Display ✅ DONE

**Goal:** Ensure country and currency metadata flows correctly from DB records (both NetSuite and web-sourced) into the RFQ display.

### 5.1 Already handled ✅

The `_enrich_rfq_supplier_contacts` function (updated in Phase 4.3) already pulls country from the DB. With web-sourced suppliers now persisted to the DB (Phase 4.2), the enrichment will naturally find them and populate the RFQ display with correct metadata on subsequent loads.

---

## Testing

> **Status:** All 340 existing tests pass. Unit tests for the new functionality (listed below) have NOT been written yet.

### Manual testing ✅ (done during development)
1. Open an RFQ with a line item that has no internal suppliers (or create a new one)
2. Click "Find Suppliers" on the line item
3. Verify the Research Agent:
   - Searches for Australian suppliers first
   - Identifies country (2-letter code) for each supplier
   - Identifies currency for each supplier
   - Classifies each supplier's tier and category
   - Stores original foreign prices without converting to AUD
   - Does NOT falsely match a web-sourced supplier to an existing DB supplier with a similar name but different domain/country
   - Creates a new Supplier DB record for genuinely new suppliers (`source='web'`)
4. Verify the RFQ display shows:
   - Country code after supplier name (for non-AU suppliers)
   - Tier badge
   - Correct currency on any pricing
   - Blue database icon for NetSuite-synced suppliers, amber for web-sourced suppliers
5. Find suppliers on a second RFQ line for the same product — verify web-sourced suppliers from step 3 are now found via DB match (no re-classification)

### Unit tests
- Add tests for `match_supplier()` in `database.py`: verify domain mismatch rejects, country mismatch rejects, and matching domain+country accepts
- Update `tests/tools/test_quote_tools.py` to verify `_match_suppliers_to_db` passes URL/country and rejects false positives
- Update `tests/tools/test_quote_tools.py` to verify `_match_suppliers_to_db` creates new Supplier records with `source='web'` when no match
- Update `tests/tools/test_quote_tools.py` to verify `add_supplier` preserves `country`, `currency`, `tier`, and `category` fields
- Update `tests/test_dashboard_routes.py` to verify `_normalize_rfq_suppliers` includes the new default fields

---

## Summary of Changes by File

| File | Changes |
|------|---------|
| `includes/dashboard/models.py` | Add `source` column to Supplier model |
| `alembic/versions/` | Migration to add `source` column, backfill as `'netsuite'` |
| `includes/dashboard/database.py` | Add `_extract_domain()` utility (strips subdomains, handles two-part ccTLDs like `.com.au`, `.co.uk`). Add `match_supplier()` with domain-first lookup + domain/country verification |
| `includes/tools/quote_tools.py` | Update `_match_suppliers_to_db` to use `match_supplier()` with URL/country; create new Supplier records for unmatched web suppliers; auto-categorize new suppliers via `categorize_supplier()`; URL verification + correction via HTTP HEAD + Gemini search fallback with product context |
| `app.py` | Update Phase 2 prompt: geo-preference, country/currency requirements; simplified taxonomy (auto-categorization) |
| `includes/prompts.py` | Update `RFQ_WORKFLOW_PROMPT`: mandatory country/currency, geo-preference, no AUD conversion; tier/category made optional (auto-classified server-side) |
| `includes/dashboard/routes.py` | Add `country`, `currency`, `tier`, `category`, `source`, `is_new` to `_SUPPLIER_DEFAULTS`. Update `_enrich_rfq_supplier_contacts` to fetch `source`, check Transaction history for `is_new` flag, mark suppliers without `supplier_id` as new |
| `templates/partials/rfq_detail.html` | DB icon = has transaction history (blue); NEW badge (green) = no transaction history; mutually exclusive display |
| `scripts/sync_netsuite_suppliers.py` | Add dedup check against `source='web'` suppliers (pre-loaded indexes by name/domain) before creating new records; merge if match found; new inserts set `source='netsuite'` |
| `tests/tools/test_quote_tools.py` | ⚠️ NOT YET DONE — test new fields, stricter matching, Supplier record creation |
| `tests/test_dashboard_routes.py` | ⚠️ NOT YET DONE — test new defaults in normalization |

---

## Additional Work (Beyond Original Plan)

### Domain extraction improvements ✅
- `_extract_domain()` now strips ALL subdomains (not just `www.`), handling two-part ccTLDs (`.com.au`, `.co.uk`, `.co.nz`, etc.). Example: `my.komatsu.com.au` → `komatsu.com.au`

### Domain-first supplier matching ✅
- `match_supplier()` now does domain-first lookup before name matching. If a URL is provided, it searches all existing suppliers for a matching root domain. Same domain = same business, regardless of name differences (e.g. "Repco Australia" matches "Repco Export & Wholesale" via `repco.com.au`).

### Full auto-categorization of new web suppliers ✅
- Instead of embedding a condensed taxonomy in the agent prompt (Phase 3 original plan), new web suppliers are categorized server-side using the full `categorize_supplier()` pipeline from `includes/supplier_categorization.py` — same as the batch script. Uses complete taxonomy, search-grounded Gemini, confidence scoring, and category validation.

### NEW badge for suppliers without transaction history ✅
- Added `is_new` flag computed during enrichment. Suppliers with no Quotes or Sales Orders in the Transaction table get a green **NEW** badge. Suppliers not in the DB at all are also marked NEW. The DB icon (blue) now means "has previous transaction history" — it's mutually exclusive with NEW.

### URL verification and correction ✅
- Before matching or persisting a supplier, the URL is verified via HTTP HEAD request (5s timeout).
- If the URL fails (connection refused, timeout, non-200, parked domain with 204), Gemini with Google Search grounding is used to find the correct website.
- The search prompt includes product context (part number, brand, description) for better results.
- The corrected URL is also HTTP-verified before accepting.
- If nothing better is found, the original URL is kept (never leaves the supplier with no data).
