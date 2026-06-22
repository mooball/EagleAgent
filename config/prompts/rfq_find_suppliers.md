# Skill Definition: RFQ Find Suppliers

**Web-based Supplier Discovery (v1.0)**

Search the web for distributors and wholesalers who can supply a given product.

## Step 0 — Search local database first (ALWAYS)

Before any web search, you MUST first check our internal database:
1. Search products by part number or brand using `search_products`
2. Check purchase history using `part_purchase_history`
3. Search existing suppliers using `search_suppliers`
4. **Once the local search is complete, ALWAYS ask the user:** "Would you like me to search the internet for additional suppliers?"
5. Only proceed to the web search below if the user says yes. Never search the web preemptively.

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
Prioritise authorised distributors and industrial wholesalers over retail sources. If distributors are scarce, include reputable retailers as fallback options. Aim for 3–5 good supplier options but more is fine if they look like strong matches.

## Supply Chain Classification
Do NOT attempt to categorize suppliers yourself. The system will automatically classify each new supplier using our full taxonomy after you add them. You may optionally include `tier` (A/B/C/D) and `category` (e.g. 'Trade Wholesaler') if it is obvious, but the system will verify and correct these.

## Adding Suppliers to the RFQ
CRITICAL: After researching, you MUST call `manage_rfq(action='add_supplier')` to add each supplier you find to the RFQ. Each supplier dict must include:
- `name` — supplier name
- `country` — 2-letter ISO code (e.g. 'AU', 'US', 'GB'). REQUIRED.
- `currency` — 3-letter ISO code for their trading currency (e.g. 'AUD', 'USD', 'GBP'). REQUIRED.
- `contacts` — list with at least one of email/phone/url. REQUIRED.

Optional fields: `tier`, `category`, `price`, `price_type`, `lead_time`, `notes`.

If a price is in a foreign currency, store the ORIGINAL price and set currency accordingly — do NOT convert to AUD.

If you do NOT call `add_supplier`, the suppliers will NOT appear on the RFQ. The user is counting on you to update the RFQ directly.
