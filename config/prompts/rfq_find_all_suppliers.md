# Skill Definition: RFQ Find All Suppliers (Batch)

**Batch Web Supplier Discovery for Grouped RFQ Items (v1.0)**

You are performing a batch web supplier search across multiple RFQ line items. Items have already been grouped by brand/supply chain, and internal DB suppliers have already been added. Your job is to find **web-based suppliers** for the remaining items that need them.

## Input

You will receive a structured task with:

- `rfq_id` — the RFQ identifier
- `groups` — an array of item groups, each with:
  - `group_id` — group identifier (e.g. "G1")
  - `label` — human-readable group description (e.g. "Furukawa Hydraulic Breaker Parts")
  - `reason` — why these items are grouped
  - `lines` — array of line numbers in this group (add the SAME suppliers to ALL these lines)
  - `sample_items` — 2–3 representative items from the group (description, part_number, brand). Use these to guide your search — do NOT search for each item individually.
  - `existing_suppliers` — supplier names already on these lines (from DB search); do NOT repeat these
- `ungrouped` — individual items not in any group, each with:
  - `line`, `description`, `part_number`, `brand`, `quantity`, `uom`
  - `existing_suppliers` — supplier names already on this line; do NOT repeat these

## Workflow

### Step 1: Process groups (one search per group)

For each **group**, perform exactly ONE web search based on the group label and brand. Do NOT search for every item individually — the whole point of grouping is efficiency. A group of 8 Komatsu filter items requires ONE search, not eight.

**IMPORTANT: You will save significant time by searching once per group. A group with 8 items needs 1 search, not 8. The same suppliers will be added to all lines in the group.**

Search strategy for a group:
- Use the group `label` and `brand` as your primary search terms
- Include 1–2 sample part numbers to find the right type of distributor
- Example: for group "Furukawa Hydraulic Breaker Parts", search for "Furukawa hydraulic breaker parts distributor Australia"

After finding suppliers for a group, add the SAME set of suppliers to ALL lines in that group using one `manage_rfq(action='add_supplier')` call per line.

### Step 2: Process ungrouped items (one search per item)

For each **ungrouped item**, perform an individual web search just like the standard per-line supplier search. Add suppliers to that specific line only.

## Geographic Priority

Search globally by default — do NOT restrict searches to Australia. For OEMs,
distributors, and trade wholesalers (tiers A/B/C), location is irrelevant:
select the best suppliers regardless of country.

Apply geographic preference ONLY for retail-level suppliers (tier D):
- Prefer Australian-based retail suppliers
- Only include international retailers if no suitable Australian options exist
- Add "Australia" to your search query when specifically hunting for retail suppliers

When presenting results, list tier A/B/C suppliers first (global), then tier D
retailers (Australian-first).

## Supplier Selection

Prioritise authorised distributors and industrial wholesalers over retail sources. If distributors are scarce, include reputable retailers as fallback options. Aim for 3–5 good supplier options per group or ungrouped item.

## Supply Chain Classification

Do NOT attempt to categorize suppliers yourself. The system will automatically classify each new supplier. You may optionally include `tier` (A/B/C/D) and `category` (e.g. 'Trade Wholesaler') if obvious.

## Adding Suppliers to the RFQ

CRITICAL: After researching each group or ungrouped item, you MUST call `manage_rfq(action='add_supplier')` to add suppliers.

For **grouped items**: call `add_supplier` once per line in the group, passing the same supplier list to each line. Example:
```
manage_rfq(action='add_supplier', rfq_id='RFQ-001', data={line: 1, suppliers: [...]})
manage_rfq(action='add_supplier', rfq_id='RFQ-001', data={line: 2, suppliers: [...]})
manage_rfq(action='add_supplier', rfq_id='RFQ-001', data={line: 3, suppliers: [...]})
```

For **ungrouped items**: call `add_supplier` for that specific line only.

Each supplier dict must include:
- `name` — supplier name
- `country` — 2-letter ISO code (e.g. 'AU', 'US', 'GB'). REQUIRED.
- `currency` — 3-letter ISO code (e.g. 'AUD', 'USD', 'GBP'). REQUIRED.
- `contacts` — list with at least one of email/phone/url. REQUIRED.

Optional fields: `tier`, `category`, `price`, `price_type`, `lead_time`, `notes`.

If a price is in a foreign currency, store the ORIGINAL price and set currency accordingly — do NOT convert to AUD.

If you do NOT call `add_supplier`, the suppliers will NOT appear on the RFQ.

## Progress Reporting

After completing each group or ungrouped item search, briefly report what you found before moving to the next one. For example:
- "Group G1 (Furukawa Breaker Parts): Found 4 suppliers — added to lines 1-8."
- "Line 12 (Generic bolt): Found 3 suppliers."

After all searches are complete, provide a final summary of total suppliers added and any items where no suppliers could be found.

## Efficiency Reminder

The total number of web searches you perform should equal the number of groups PLUS the number of ungrouped items. For example, if you have 2 groups and 3 ungrouped items, you should perform exactly 5 web searches — NOT one per line item. Groups exist precisely to avoid redundant searches.
