# Plan: Supplier Categorization (R&D)

Classify suppliers into supply chain roles (OEM, Wholesaler, Dealer, etc.) using a search-grounded Gemini LLM and the taxonomy defined in `docs/supplier-categorization-taxonomy.md`.

## Status: In Progress

## Context

We have ~9,500 suppliers in the database. Many have a URL on file. We want to automatically categorize each supplier into one of 8 roles from our taxonomy (OEM, Aftermarket Manufacturer, Trade Wholesaler, Authorized Dealer, Retail/Trade Outlet, Online Distributor, Service Exchange Provider, Sourcing Broker).

The approach uses Google Search Grounding with Gemini to visit/analyze supplier websites and apply the categorization decision logic from the taxonomy.

## Phase 1: Proof of Concept (current)

### 1. ✅ Convert taxonomy PDF to Markdown
   - Output: `docs/supplier-categorization-taxonomy.md`

### 2. Write a script to extract top 50 suppliers by purchase volume
   - Query the database for the 50 suppliers with the most purchase/order transactions.
   - Output: `data/top_50_suppliers.json` — array of `{id, name, url, city, country, purchase_count}`.
   - Script: `scripts/extract_top_suppliers.py`

### 3. Write a categorization script using search-grounded Gemini
   - Script: `scripts/categorize_suppliers.py`
   - Reads `data/top_50_suppliers.json` (or accepts `--input` path).
   - For each supplier, sends a prompt to Gemini with:
     - The full taxonomy (from `docs/supplier-categorization-taxonomy.md`).
     - The supplier name, URL, city, and country.
     - Google Search Grounding enabled so the LLM can look up the supplier's website.
   - The LLM must return structured JSON per supplier:
     ```json
     {
       "supplier_id": 123,
       "supplier_name": "Example Pty Ltd",
       "category": "Trade Wholesaler",
       "tier": "B",
       "confidence": 4,
       "reasoning": "Website requires ABN and trade account application..."
     }
     ```
   - Output: `data/supplier_categories.json` — array of results.
   - Support `--model` flag to switch between `gemini-3-flash-preview` and `gemini-3.1-pro-preview` for comparison.
   - Add `--dry-run` flag to show the prompt for the first supplier without calling the API.
   - Add rate limiting (e.g. 1 request/sec) to stay within API quotas.

### 4. Manual review of results
   - Compare Flash vs Pro outputs for accuracy and confidence calibration.
   - Identify taxonomy gaps or ambiguous cases.
   - Refine the taxonomy markdown and re-run as needed.

## Phase 2: Integration (future, after taxonomy is stable)

### 5. Add `category` column to the `suppliers` table
   - Alembic migration adding `category VARCHAR`, `category_confidence INT`, `category_reasoning TEXT`.

### 6. Build categorization into the agent toolset
   - Add a tool or background job that categorizes new/uncategorized suppliers.
   - Display category in supplier search results.

### 7. Bulk categorization of all suppliers
   - Extend the script to process all ~9,500 suppliers in batches.
   - Store results directly in the database.

## Files

| File | Purpose |
|------|---------|
| `docs/supplier-categorization-taxonomy.md` | Taxonomy definition (source of truth) |
| `scripts/extract_top_suppliers.py` | Extract top 50 suppliers by purchase volume |
| `scripts/categorize_suppliers.py` | Run Gemini categorization on supplier list |
| `data/top_50_suppliers.json` | Extracted supplier list (gitignored) |
| `data/supplier_categories.json` | Categorization results (gitignored) |

## Notes

- `data/` is gitignored, so results files won't be committed.
- The taxonomy markdown IS committed and serves as the prompt source of truth.
- Google Search Grounding is required — the LLM needs to access supplier websites to make accurate categorizations.
- Suppliers without a URL will rely on name-based web search only; expect lower confidence scores.
