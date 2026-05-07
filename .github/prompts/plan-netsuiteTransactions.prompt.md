## Plan: NetSuite Transactions Sync (Sales Orders & Quotes)

### Overview

Pull Sales Order and Quote transaction line items from NetSuite into the local database. This builds on the existing NetSuite integration (`plan-netsuiteIntegration.prompt.md`) — auth, client, streaming pagination, batch commits, and `--resume` support are all already in place.

**Key goals:**
- Sync Sales Order line items (purchase cost + sales price per product/supplier)
- Sync Quote line items (quoted prices per product/supplier)
- Rename `ProductSupplier` model → `Transaction` to reflect broader scope
- Add `cost` and `cost_currency` columns
- Replace the legacy CSV import scripts with API-based sync

---

### Current State

**`ProductSupplier` model** (`includes/dashboard/models.py`):
```python
class ProductSupplier(Base):
    __tablename__ = 'product_suppliers'
    id = Column(UUID, primary_key=True)
    doc_number = Column(String, nullable=False, index=True)
    date = Column(Date, nullable=True)
    product_id = Column(UUID, ForeignKey('products.id'), nullable=False, index=True)
    supplier_id = Column(UUID, ForeignKey('suppliers.id'), nullable=False, index=True)
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)       # sales price
    status = Column(String, nullable=True)
```

**Files that reference `ProductSupplier` or `product_suppliers`:**
- `includes/dashboard/models.py` — model definition
- `includes/dashboard/routes.py` — dashboard queries (6+ references)
- `includes/tools/product_tools.py` — agent tool queries (15+ references)
- `scripts/import_purchase_history.py` — legacy CSV import
- `scripts/import_quote_history.py` — legacy CSV import
- `scripts/extract_top_suppliers.py` — supplier stats
- `scripts/categorize_suppliers_job.py` — categorization queries
- `scripts/sync_prod_data.py` — prod data sync table list
- `copilot-instructions.md` — documentation
- `EagleAgent.code-workspace` — saved terminal commands
- `alembic/versions/` — 2 existing migrations
- `templates/` — UI refers to "purchases" (not the model name directly)

---

### Phase 0: API Discovery (SuiteQL Queries) ✅ DONE

Discovery confirmed the following structure for both Sales Orders and Quotes.

**Common structure:** Both use `transaction` (parent) + `transactionLine` (child items) tables with the same custom columns.

**Confirmed SuiteQL query pattern:**
```sql
SELECT t.tranid, t.trandate, t.status,
  BUILTIN.DF(t.currency) AS currency_name,
  t.lastmodifieddate,
  tl.item, BUILTIN.DF(tl.item) AS item_name,
  tl.quantity, tl.rate,
  tl.custcol_po_rate, tl.custcol_po_vendor,
  BUILTIN.DF(tl.custcol_po_vendor) AS vendor_name,
  tl.uniquekey, tl.linelastmodifieddate
FROM transactionLine tl
INNER JOIN transaction t ON t.id = tl.transaction
WHERE t.type = 'SalesOrd'  -- or 'Estimate' for quotes
  AND tl.item IS NOT NULL
  AND tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND tl.custcol_po_vendor IS NOT NULL
  AND t.lastmodifieddate >= '{ns_date}'
ORDER BY t.lastmodifieddate ASC
```

**Confirmed field mappings:**

| NetSuite Field | Local Column | Notes |
|---|---|---|
| `t.tranid` | `doc_number` | e.g. "S247000", "Q64644" |
| `t.trandate` | `date` | Format: d/m/yyyy |
| `t.status` | `status` | Single letter code (see enum below) |
| `BUILTIN.DF(t.currency)` | `cost_currency` | e.g. "AUD" |
| `tl.item` → match `products.netsuite_id` | `product_id` | |
| `tl.custcol_po_vendor` → match `suppliers.netsuite_id` | `supplier_id` | |
| `ABS(tl.quantity)` | `quantity` | Values are negative in NetSuite (outbound convention) |
| `tl.rate` | `price` | Sell/quoted price |
| `tl.custcol_po_rate` | `cost` | Purchase/cost price |
| `tl.uniquekey` | `netsuite_id` | Globally unique per line — use for upsert |
| `t.lastmodifieddate` | `netsuite_last_modified` | For `--resume` support |

**Filter rules:**
- `tl.mainline = 'F'` — exclude header summary lines
- `tl.taxline = 'F'` — exclude tax lines
- `tl.custcol_po_vendor IS NOT NULL` — skip freight/fees/misc lines without a vendor

**Volume:**
- Sales Order lines: **149,339** (124K with vendor = 83%)
- Quote lines: **310,133** (265K with vendor = 86%)
- Total: **~460K lines** (after vendor filter: ~389K)

**Status codes (from `transactionStatus` table):**

Sales Orders (`SalesOrd`):
| Code | Name | Friendly Key |
|------|------|--------------|
| `A` | Pending Approval | `pendingApproval` |
| `B` | Pending Fulfillment | `pendingFulfillment` |
| `C` | Cancelled | `cancelled` |
| `D` | Partially Fulfilled | `partiallyFulfilled` |
| `E` | Pending Billing/Partially Fulfilled | `pendingBillingPartFulfilled` |
| `F` | Pending Billing | `pendingBilling` |
| `G` | Billed | `fullyBilled` |
| `H` | Closed | `closed` |

Quotes (`Estimate`):
| Code | Name | Friendly Key |
|------|------|--------------|
| `A` | Open | `open` |
| `B` | Processed | `processed` |
| `C` | Closed | `closed` |
| `V` | Voided | `voided` |
| `X` | Expired | `expired` |

**Implementation note:** Store the raw letter code in the `status` column. Define Python enum/dict mappings per `doc_type` in a shared module (e.g. `includes/netsuite/constants.py`) for display translation.

---

### Phase 1: Rename `ProductSupplier` → `Transaction` ✅ DONE

Class renamed across codebase (Option A). Table remains `product_suppliers`. Backwards alias `ProductSupplier = Transaction` retained. All 324 tests pass.

**Option A: Rename class only, keep table name** (recommended)
- Rename `ProductSupplier` → `Transaction` in Python code
- Keep `__tablename__ = 'product_suppliers'` (no migration needed for the rename itself)
- Update all imports and references across the codebase
- Add `# Legacy table name retained for backwards compatibility` comment

**Option B: Rename table too** (more disruptive)
- Alembic migration: `ALTER TABLE product_suppliers RENAME TO transactions`
- Rename all indexes
- Higher risk on production

**Recommendation:** Option A — rename the class only. The table name is an implementation detail that doesn't need to match the class name.

**Files to update for Option A:**
| File | Change |
|------|--------|
| `includes/dashboard/models.py` | Rename class, keep `__tablename__` |
| `includes/dashboard/routes.py` | Update imports + ~10 query references |
| `includes/tools/product_tools.py` | Update imports + ~20 query references |
| `scripts/import_purchase_history.py` | Update import |
| `scripts/import_quote_history.py` | Update import |
| `scripts/extract_top_suppliers.py` | Update import + references |
| `scripts/categorize_suppliers_job.py` | Update import + references |
| `scripts/sync_prod_data.py` | Update table name in list |
| `copilot-instructions.md` | Update documentation |
| `tests/` | Update any test references |

---

### Phase 2: Add New Columns (Migration) ✅ DONE

Migration `39e2882667f4_add_transaction_columns` applied. Adds `doc_type`, `netsuite_id` (unique), `cost`, `cost_currency`, `netsuite_last_modified`. Backfills existing rows with `doc_type = 'PurchaseOrder'`. Also added `IGNORED_COLUMNS` config to `alembic/env.py` to suppress spurious `hubspot_id` index/constraint diffs.

Original column plan:

```python
class Transaction(Base):
    __tablename__ = 'product_suppliers'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_number = Column(String, nullable=False, index=True)
    doc_type = Column(String, nullable=True, index=True)    # NEW: 'SalesOrder', 'Quote', 'PurchaseOrder'
    netsuite_id = Column(String, unique=True, nullable=True) # NEW: NetSuite transaction line unique ID
    date = Column(Date, nullable=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey('products.id'), nullable=False, index=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey('suppliers.id'), nullable=False, index=True)
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)                     # sales price
    cost = Column(Float, nullable=True)                      # NEW: purchase/cost price
    cost_currency = Column(String(3), nullable=True)         # NEW: e.g. 'AUD', 'USD'
    status = Column(String, nullable=True)
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)  # NEW: for --resume
```

**New columns:**
- `doc_type` — distinguishes Sales Orders from Quotes from legacy Purchase Orders
- `netsuite_id` — unique key for upsert (transaction line ID from NetSuite)
- `cost` — purchase/cost price from the Sales Order
- `cost_currency` — currency code (ISO 4217)
- `netsuite_last_modified` — enables `--resume` pattern

**Alembic migration:**
```python
op.add_column('product_suppliers', sa.Column('doc_type', sa.String(), nullable=True))
op.add_column('product_suppliers', sa.Column('netsuite_id', sa.String(), nullable=True))
op.add_column('product_suppliers', sa.Column('cost', sa.Float(), nullable=True))
op.add_column('product_suppliers', sa.Column('cost_currency', sa.String(3), nullable=True))
op.add_column('product_suppliers', sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True))
op.create_index('ix_product_suppliers_netsuite_id', 'product_suppliers', ['netsuite_id'], unique=True)
op.create_index('ix_product_suppliers_doc_type', 'product_suppliers', ['doc_type'])
```

**Backfill existing data:**
- Set `doc_type = 'PurchaseOrder'` for all existing rows (they came from PO CSV imports)

---

### Phase 3: Sales Order Sync Script ✅ DONE

`scripts/sync_netsuite_sales_orders.py`

Pattern identical to `sync_netsuite_products.py`:
- `--since` / `--resume` / `--dry-run` flags
- Streaming pagination via `suiteql_iter()`
- Batch commits every `NETSUITE_SYNC_BATCH_SIZE` rows
- ASC sort on `lastmodifieddate` for resumability
- Upsert on `netsuite_id`
- Sets `doc_type = 'SalesOrder'`

**Mapping (confirmed):**
| NetSuite Field | Local Column |
|----------------|--------------|
| `t.tranid` | `doc_number` |
| `t.trandate` | `date` |
| `t.status` | `status` |
| `BUILTIN.DF(t.currency)` | `cost_currency` |
| `tl.item` → match `products.netsuite_id` | `product_id` |
| `tl.custcol_po_vendor` → match `suppliers.netsuite_id` | `supplier_id` |
| `ABS(tl.quantity)` | `quantity` |
| `tl.rate` | `price` (sell price) |
| `tl.custcol_po_rate` | `cost` (purchase price) |
| `t.lastmodifieddate` | `netsuite_last_modified` |
| `tl.uniquekey` | `netsuite_id` |

**Filters:** `mainline = 'F'`, `taxline = 'F'`, `custcol_po_vendor IS NOT NULL`

---

### Phase 4: Quote Sync Script ✅ DONE

`scripts/sync_netsuite_quotes.py`

Same pattern as Phase 3 but for Estimates:
- `doc_type = 'Quote'`
- `WHERE t.type = 'Estimate'`
- Quotes have cost data too (confirmed: `custcol_po_rate` populated on 86% of lines)
- Status: Open / Processed / Closed / Voided / Expired

---

### Phase 5: Status Enums & Constants ✅ DONE

Created `includes/netsuite/constants.py` with status mappings:

```python
SALES_ORDER_STATUS = {
    "A": "Pending Approval",
    "B": "Pending Fulfillment",
    "C": "Cancelled",
    "D": "Partially Fulfilled",
    "E": "Pending Billing/Partially Fulfilled",
    "F": "Pending Billing",
    "G": "Billed",
    "H": "Closed",
}

QUOTE_STATUS = {
    "A": "Open",
    "B": "Processed",
    "C": "Closed",
    "V": "Voided",
    "X": "Expired",
}

# Reverse lookups for display
def get_status_label(doc_type: str, code: str) -> str:
    if doc_type == "SalesOrder":
        return SALES_ORDER_STATUS.get(code, code)
    elif doc_type == "Quote":
        return QUOTE_STATUS.get(code, code)
    return code
```

Use this in dashboard routes and agent tools to show human-readable status.

---

### Phase 6: Register Scripts & Cleanup ✅ DONE

1. Added `sync_netsuite_sales_orders` and `sync_netsuite_quotes` to `config/scripts.py` SCRIPT_REGISTRY (with `--since`, `--resume`, `--dry-run`)
2. Added `sales_orders_updated_since()` and `quotes_updated_since()` query builders to `includes/netsuite/queries.py`
3. Removed legacy CSV import scripts: `scripts/import_purchase_history.py` and `scripts/import_quote_history.py`
4. Removed `import_purchase_history` from script registry
5. Updated `docs/SERVER_SCRIPTS.md`

---

### Phase 7: Dashboard & Agent Updates ✅ DONE (partial)

Dashboard route updates completed:
1. Renamed `/purchases` → `/transactions` (including `/partial/transactions` and `/partial/transactions/rows`)
2. Added Cost column — formatted as `$12,000.00` (AUD) or `$12,000.00 USD` (non-AUD)
3. Price column now shows `$` prefix with comma-separated thousands
4. Status column shows human-readable labels via `get_status_label()`
5. No doc_type column (redundant — obvious from doc number prefix)
6. Renamed templates: `purchases.html` → `transactions.html`, partials likewise
7. Updated nav in `base.html` and home tile in `home.html`

Still TODO (deferred):
- Update agent tools to leverage cost data in supplier comparisons

---

### Decisions Made

1. **Table rename vs class rename** — Option A confirmed: rename class only, keep `__tablename__ = 'product_suppliers'`
2. **Supplier on Sales Orders** — resolved: `tl.custcol_po_vendor` is the vendor ID (custom column "PO Vendor")
3. **Cost field** — use `tl.custcol_po_rate` (custom column "PO Rate")
4. **Lines without vendor** — skip (freight/fees/misc, not useful for our purpose)
5. **Quote status filtering** — import ALL statuses from the API (store the code, display via enum)
6. **Volume** — ~389K lines after vendor filter (124K SO + 265K Quotes) — manageable with streaming+batch+resume
7. **Existing data** — TBD: decide after API sync is working whether to re-import or keep legacy rows

---

### Execution Order

```
Phase 1 (Rename)       →  ✅ DONE
Phase 2 (Migration)    →  ✅ DONE
Phase 5 (Constants)    →  ✅ DONE
Phase 3 (Sales Orders) →  ✅ DONE
Phase 4 (Quotes)       →  ✅ DONE
Phase 6 (Registry)     →  ✅ DONE
Phase 7 (Dashboard)    →  ✅ DONE (agent tools deferred)
```
