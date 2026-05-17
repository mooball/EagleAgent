# Skill Definition: RFQ Find Suppliers

**Web-based Supplier Discovery (v1.0)**

Search the web for distributors and wholesalers who can supply a given product.

## Geographic Priority
1. FIRST search for Australian-based suppliers (add 'Australia' to your search queries).
2. If fewer than 3 Australian suppliers are found, expand to international suppliers.
3. When listing results, present Australian suppliers first, then international.

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
