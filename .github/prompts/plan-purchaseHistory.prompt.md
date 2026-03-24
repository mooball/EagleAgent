## Plan: Purchase History Table, Import & Search Tool

### Overview

Add a `product_suppliers` table that records historical purchase transactions, acting as a join between `products` and `suppliers`. Create an import script for CSV data that matches part numbers and supplier names to their database IDs. Add a `search_purchase_history` tool to the ProcurementAgent so it can answer questions like _"who can supply product X?"_ or _"which suppliers have we purchased part Y from?"_.

---

### Phase 1: Database Schema

1. **Add `ProductSupplier` model** to `includes/db_models.py`:
   - `id` — UUID primary key
   - `netsuite_id` — String, not null, unique constraint (`uq_product_supplier_netsuite`) — dedup key to prevent re-importing the same purchase record
   - `date` — Date, nullable
   - `product_id` — UUID, FK → `products.id`, not null, indexed
   - `quantity` — Float, nullable
   - `price` — Float, nullable
   - `supplier_id` — UUID, FK → `suppliers.id`, not null, indexed
   - `status` — String, nullable

2. **Create Alembic migration**:
   ```bash
   uv run alembic revision --autogenerate -m "add_product_suppliers_table"
   uv run alembic upgrade head
   ```

---

### Phase 2: Import Script — `scripts/import_purchase_history.py`

Follow the same patterns as `scripts/import_products.py` and `scripts/import_suppliers.py`:

1. Accept `--production` flag via argparse.
2. Read CSV file(s) matching `purchase_history_import*.csv` from `Config.IMPORT_DIR`.

**CSV columns:**

| CSV Column   | Type       | Maps To                          |
|-------------|------------|----------------------------------|
| netsuite_id | string     | `product_suppliers.netsuite_id`  |
| date        | dd/mm/yyyy | `product_suppliers.date`         |
| status      | string     | `product_suppliers.status`       |
| part_number | string     | Lookup → `product_suppliers.product_id` |
| quantity    | numeric    | `product_suppliers.quantity`     |
| price       | numeric    | `product_suppliers.price`        |
| supplier    | string     | Lookup → `product_suppliers.supplier_id` |

3. **Data cleaning**:
   - Strip whitespace on all string fields.
   - Parse `date` from `dd/mm/yyyy` format using `pd.to_datetime(format='%d/%m/%Y')`.
   - Cast `quantity` and `price` to float safely.

4. **Pre-build lookup caches** to avoid per-row queries:
   - Query all products into a dict: `{part_number.lower(): product.id}`.
   - Query all suppliers into a dict: `{name.lower(): supplier.id}`.
   - If multiple products share the same part number (different brands), use the first match and log a warning.

5. **Matching logic** — the CSV provides human-readable identifiers, not UUIDs:
   - **Part number → Product ID**: match `part_number` against `products.part_number` (case-insensitive). Skip and log unmatched rows.
   - **Supplier name → Supplier ID**: match `supplier` against `suppliers.name` (case-insensitive). Skip and log unmatched rows.

6. **Upsert logic** keyed on `netsuite_id`:
   - If a record with the same `netsuite_id` exists → update.
   - Otherwise → insert new record.

7. Batch processing (200 per batch).
8. Summary output: imported count, skipped count, unmatched part numbers listed, unmatched supplier names listed.

---

### Phase 3: Search Tool — `search_purchase_history`

1. **Add `search_purchase_history` tool** to `includes/tools/product_tools.py`:
   - Accept parameters: `part_number` (str), `limit` (int, default 20).
   - Run a single SQL query that aggregates purchase history per supplier for the matched part number.

2. **SQL query** — encapsulate all aggregation so the agent receives ready-to-display data:
   ```sql
   SELECT
       s.name                          AS supplier_name,
       MAX(ps.date)                    AS most_recent_date,
       (
           SELECT ps2.price
           FROM product_suppliers ps2
           WHERE ps2.supplier_id = s.id
             AND ps2.product_id = p.id
           ORDER BY ps2.date DESC
           LIMIT 1
       )                               AS most_recent_price,
       SUM(ps.quantity)                AS total_quantity,
       COUNT(ps.id)                    AS order_count
   FROM product_suppliers ps
   JOIN products p ON ps.product_id = p.id
   JOIN suppliers s ON ps.supplier_id = s.id
   WHERE p.part_number ILIKE :part_number
   GROUP BY s.id, s.name, p.id
   ORDER BY total_quantity DESC
   LIMIT :limit;
   ```

3. **Return format** — formatted as a markdown table the agent can display directly:
   ```
   Purchase history for part number '123-ABC':

   Found 3 suppliers, sorted by total quantity supplied:

   | # | Supplier | Last Price | Last Date | Total Qty | Orders |
   |---|----------|-----------|-----------|-----------|--------|
   | 1 | Acme Tools | $42.50 | 15/01/2026 | 500 | 12 |
   | 2 | Global Parts | $44.00 | 03/11/2025 | 200 | 5 |
   | 3 | Smith & Co | $39.99 | 20/06/2024 | 50 | 2 |
   ```

4. **Register the tool** on `ProcurementAgent` in `includes/agents/procurement_agent.py`:
   - Add `search_purchase_history` to the imports and `get_tools()` return list.
   - Add to the system prompt's **Available Tools** section:
     ```
     - search_purchase_history(part_number: str, limit: int):
       Search past purchase records to find which suppliers have supplied a given part.
       Returns a per-supplier summary: supplier name, most recent price, most recent
       supply date, total quantity ever purchased, and number of orders. Sorted by
       total quantity descending. Use when the user asks "who can supply part X?",
       "which suppliers have we bought part X from?", or similar.
     ```

---

### Implementation Order

| Step | Task                                            | Files Changed                                        |
|------|-------------------------------------------------|------------------------------------------------------|
| 1    | Add `ProductSupplier` model                     | `includes/db_models.py`                              |
| 2    | Generate & run Alembic migration                | `alembic/versions/xxx_add_product_suppliers_table.py` |
| 3    | Create import script                            | `scripts/import_purchase_history.py`                 |
| 4    | Place CSV in import directory                   | `data/import/purchase_history_import_01.csv`         |
| 5    | Run import, verify data                         | (terminal)                                           |
| 6    | Add `search_purchase_history` tool              | `includes/tools/product_tools.py`                    |
| 7    | Register tool & update prompt in agent          | `includes/agents/procurement_agent.py`               |
| 8    | Test end-to-end via agent                       | (manual or integration test)                         |

---

### Edge Cases & Considerations

- **Unmatched part numbers**: Rows skipped and logged. Review log output and add missing products first if needed.
- **Unmatched supplier names**: Rows skipped and logged. Supplier names in the CSV may not exactly match `suppliers.name` (e.g. "Acme" vs "Acme Pty Ltd"). Consider using `ilike` with `%` wildcards if exact matching has a low hit rate.
- **Duplicate netsuite_ids**: Unique constraint prevents re-importing. Upsert logic updates existing records on re-run.
- **Multiple products for same part_number**: Products have composite unique key `(part_number, brand)` but the CSV only provides `part_number`. First match is used, ambiguous cases logged as warnings. Acceptable for purchase history lookups since the same physical part across brands still answers "who supplies this part?".
- **Date parsing**: CSV uses `dd/mm/yyyy`. Use `pd.to_datetime(format='%d/%m/%Y')` to avoid month/day ambiguity.
- **Price/quantity as floats**: Handles decimal quantities (e.g. 1.5 metres of hose) and prices.
