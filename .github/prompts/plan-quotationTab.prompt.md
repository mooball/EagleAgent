# Plan: Quotation Tab

## Overview
Build the `/quotation` tab on the RFQ detail page. The user reviews shortlisted suppliers' incoming quotes, records cost/lead-time data, and selects the winning supplier per line item. Short-term: manual data entry by the user after reading supplier emails. Long-term: LLM-assisted parsing of incoming quotes to auto-populate fields.

## Current Problem
The `/quotation` tab renders a placeholder: "Quotation authoring workspace coming soon." There is no UI for:
- Viewing shortlisted suppliers grouped by line item in a quotation context
- Recording quoted costs, currencies, and lead times from supplier responses
- Tracking quote status (awaiting response / quoted / declined)
- Selecting the winning supplier per line
- Setting consolidated cost and sale prices at the item level

## Target State
- Quotation tab shows an items table (without Match column) with **Cost** and **Sale** columns at the item level
- Each item row expands to show only **shortlisted** suppliers
- Each supplier row shows: name, currency, terms, quoted cost, lead time, quote status badge
- Supplier quote fields (`quote_cost`, `quote_currency`, `quote_leadtime`) are editable inline
- Quote status flows: `unquoted` → `quoted` → `selected` (or `declined`)
- A **Select** button per supplier marks it as the winning quote for that line
- Item-level `cost_price` and `sale_price` are manually set by user (consolidated best price)

## Design Decisions

### Separate quote_status from supplier status
The existing `status` field on suppliers governs the sourcing pipeline (`candidate`, `shortlisted`, `dropped`). A new `quote_status` field governs the quotation workflow (`unquoted`, `quoted`, `declined`, `selected`). These are independent — a supplier can be shortlisted but unquoted, or shortlisted and quoted.

### Item-level pricing is manual
`cost_price` and `sale_price` on `RFQItem` are user-entered. Not auto-derived from supplier quotes. Gives the user full control over margins, bundle pricing, and non-system quotes.

### JSONB for supplier quote data
New fields (`quote_cost`, `quote_currency`, `quote_leadtime`, `quote_status`) are added to the existing `suppliers` JSONB array. No migration needed for these — JSONB is schema-flexible. Only the two `RFQItem` columns need a migration.

### Quote status lifecycle
```
Shortlist supplier → quote_status = "unquoted"
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
         "quoted"    "declined"   (stays "unquoted")
            │
            ▼
        "selected"
```
Only one supplier per line can be `"selected"`. Selecting a new supplier auto-deselects the previous one. Selecting an already-selected supplier deselects it (toggle behavior).

---

## Phase 1 — Data Model

### 1.1 Add `cost_price` and `sale_price` to RFQItem

**File:** `includes/dashboard/models.py` — `RFQItem` class

```python
cost_price = Column(Numeric, nullable=True)     # best supplier cost in AUD
sale_price = Column(Numeric, nullable=True)     # price quoted to customer
```

**Migration:** Auto-generated Alembic migration.

### 1.2 Add `_rfq_to_dict` serialization

**File:** `includes/tools/rfq_crud.py` — `_rfq_to_dict()`

Add to the returned item dict:
```python
"cost_price": float(item.cost_price) if item.cost_price else None,
"sale_price": float(item.sale_price) if item.sale_price else None,
```

### 1.3 Define supplier quote defaults

**File:** `includes/dashboard/routes/rfqs.py` — `_SUPPLIER_DEFAULTS`

Add:
```python
"quote_status": None,
"quote_cost": None,
"quote_currency": None,
"quote_leadtime": None,
```

### 1.4 Set `quote_status` on shortlist

**File:** `includes/dashboard/routes/rfqs.py` — `partial_rfq_shortlist_all()`, `partial_rfq_shortlist_supplier_all_items()`

When setting `status = "shortlisted"`, also set `quote_status = "unquoted"` if not already set.

**Note:** Existing shortlisted suppliers (from before this feature) will have `quote_status: null`. The UI must treat `null` the same as `"unquoted"` — no backfill migration required.

**File:** `includes/tools/rfq_crud.py` — `_add_supplier_sync()`, `_update_supplier_sync()`

Ensure `quote_status`, `quote_cost`, `quote_currency`, `quote_leadtime` are in the updatable keys list.

---

## Phase 2 — API Endpoints

### 2.1 Update item pricing

```
PATCH /partial/rfqs/{rfq_id}/items/{line}/pricing
```

**Body:** `{ "cost_price": 12.50, "sale_price": 18.00 }`
**Response:** 204 No Content
**File:** `includes/dashboard/routes/rfqs.py`

Direct column update on `RFQItem.cost_price` / `RFQItem.sale_price`. These are scalar columns — SQLAlchemy detects changes automatically (no `flag_modified()` needed).

### 2.2 Update supplier quote

```
PATCH /partial/rfqs/{rfq_id}/items/{line}/supplier-quote
```

**Body:**
```json
{
    "supplier_name": "Wurth Australia",
    "quote_status": "quoted",
    "quote_cost": 12.50,
    "quote_currency": "AUD",
    "quote_leadtime": "2 weeks"
}
```
All fields optional — only sent fields are updated.
**Response:** 204 No Content
**File:** `includes/dashboard/routes/rfqs.py`

Finds the supplier entry in `RFQItem.suppliers` by name, updates the specified quote fields, commits.

### 2.3 Select winning supplier

```
POST /partial/rfqs/{rfq_id}/items/{line}/select-supplier
```

**Body:** `{ "supplier_name": "Wurth Australia" }`
**Response:** 204 No Content
**File:** `includes/dashboard/routes/rfqs.py`

Sets `quote_status = "selected"` on the named supplier and `quote_status = "quoted"` on any previously selected supplier on the same line (reverts to `"quoted"` since they had a quote). If the named supplier is already `"selected"`, deselects it (reverts to `"quoted"`).

### 2.4 Quotation tab context

**File:** `includes/dashboard/routes/rfqs.py` — `_rfq_detail_context()`

Add quotation-specific context: items enriched with normalized supplier quote fields. The `/{rfq_id}/quotation` route already works generically via the `{tab}` parameter — just needs context enrichment.

---

## Phase 3 — UI Templates

### 3.1 New partial: `_rfq_quotation_table.html`

**File:** `templates/partials/_rfq_quotation_table.html` (new)

**Structure:**
```
Table:  Line | Description | Brand | Qty | Cost | Sale
  Row 1: 4 | Wurth widget | Wurth | 10 | 12.50 | 18.00
    └─ Expanded (shortlisted suppliers only):
       Name          | Currency | Terms | Quoted | Lead Time | Status   | Select
       Wurth Aust    | AUD      | each  | 12.50  | 2 weeks   | quoted   | [Select]
       OZ Seals      | AUD      | each  | 11.80  | 3 weeks   | quoted   | [Select]
```

**Key behaviors:**
- Item rows: `cost_price` and `sale_price` cells are click-to-edit (inline input → HTMX patch)
- Row expansion: click item row to toggle showing shortlisted/selected suppliers (filtered to `status in ("shortlisted", "selected")`)
- Supplier rows: name links to supplier detail; quote_cost, quote_currency, quote_leadtime are inline editable
- Status badge: colored pill (`unquoted`=gray, `quoted`=blue, `declined`=red, `selected`=green)
- Status cycling: click badge to cycle `unquoted` → `quoted` → `declined` → `unquoted`
- Select button: shown on `quoted` or `unquoted` suppliers; hidden on `declined`; disabled if already `selected`

### 3.2 Wire into `rfq_detail.html`

**File:** `templates/partials/rfq_detail.html`

Replace the quotation placeholder (currently lines 741–745) with:
```html
{% elif active_tab == 'quotation' %}
<div id="quotation-container">
    {% include "partials/_rfq_quotation_table.html" %}
</div>
```

No separate `x-data` scope — the quotation container lives inside the parent `rfqDetail()` scope and uses its methods directly.

### 3.3 JavaScript methods

**File:** `templates/base.html` — extend the existing `rfqDetail()` Alpine component

Add these methods to the `rfqDetail()` return object:

```javascript
updateItemPricing(line, field, value) {
    fetch('/partial/rfqs/' + this.rfqId + '/items/' + line + '/pricing', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({[field]: parseFloat(value) || null})
    });
},

updateSupplierQuote(line, supplierName, field, value) {
    var body = {supplier_name: supplierName};
    body[field] = field === 'quote_cost' ? (parseFloat(value) || null) : value;
    fetch('/partial/rfqs/' + this.rfqId + '/items/' + line + '/supplier-quote', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify(body)
    });
},

cycleQuoteStatus(line, supplierName, current) {
    var next = {unquoted: 'quoted', quoted: 'declined', declined: 'unquoted'};
    this.updateSupplierQuote(line, supplierName, 'quote_status', next[current] || 'quoted');
},

selectSupplier(line, supplierName) {
    fetch('/partial/rfqs/' + this.rfqId + '/items/' + line + '/select-supplier', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({supplier_name: supplierName})
    }).then(function() {
        htmx.ajax('GET', '/partial/rfqs/' + this.rfqId + '/quotation',
            {target: '#main-content'});
    }.bind(this));
}
```

Note: `cycleQuoteStatus` uses `|| 'quoted'` fallback to handle `null` (pre-existing suppliers without `quote_status`).

---

## Phase 4 — Implementation Order

| Step | What | Files |
|------|------|-------|
| 4.1 | Add `cost_price`, `sale_price` to `RFQItem` + migration | `models.py`, migration |
| 4.2 | Update `_rfq_to_dict` to serialize new fields | `rfq_crud.py` |
| 4.3 | Add quote defaults; set `quote_status="unquoted"` on shortlist | `rfqs.py`, `rfq_crud.py` |
| 4.4 | Add 3 new API endpoints (pricing, supplier-quote, select) | `rfqs.py` |
| 4.5 | Enrich quotation tab context in `_rfq_detail_context` | `rfqs.py` |
| 4.6 | Build `_rfq_quotation_table.html` — static table with server-rendered data | new file |
| 4.7 | Wire quotation tab into `rfq_detail.html` (replace placeholder) | `rfq_detail.html` |
| 4.8 | Add quotation methods to existing `rfqDetail()` component | `base.html` |
| 4.9 | Add inline editing UI + status toggles + select buttons | `_rfq_quotation_table.html` |

---

## Open Questions

1. **Item sale_price default** — should `sale_price` auto-populate from selected supplier's `quote_cost` with a configurable margin? Or always manual?

2. **Currency conversion** — when a supplier quotes in foreign currency, auto-convert to AUD using `includes/currency.py`? Or leave to user?

3. **Select exclusivity** — selecting a supplier on one line auto-deselects any previously selected on the SAME line. Cross-line selection is allowed (different items can have different winning suppliers). Confirm?

4. **Quote document storage** — out of scope. Revisit when LLM quote parsing is added in a future phase.
