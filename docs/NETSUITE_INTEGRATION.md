# NetSuite Integration

EagleAgent syncs supplier, product, customer, contact, opportunity, quote, sales order, and brand data from NetSuite into the local PostgreSQL database. This enables the agent to answer procurement questions with real ERP data.

## Architecture

```
NetSuite REST API (SuiteTalk)
        │
        ▼
includes/netsuite/
  ├── auth.py      — OAuth 2.0 JWT authentication + token caching
  ├── client.py    — REST client with retries, pagination, SuiteQL
  ├── queries.py   — SuiteQL query builders for each entity type
  ├── constants.py — Enum values and field mappings
  └── sync_utils.py— Deduplication and matching helpers
        │
        ▼
scripts/sync_netsuite_*.py  — One script per entity type
        │
        ▼
includes/dashboard/models.py  — SQLAlchemy ORM models
```

## Authentication

NetSuite uses **OAuth 2.0 with JWT bearer tokens** (client credentials grant, PS256 signing).

### Required Environment Variables

| Variable | Description |
|---|---|
| `NETSUITE_ACCOUNT_ID` | Your NetSuite account ID (e.g., `1234567`) |
| `NETSUITE_CLIENT_ID` | OAuth 2.0 client ID from the NetSuite integration record |
| `NETSUITE_CERTIFICATE_ID` | Certificate ID from the integration record |
| `NETSUITE_PRIVATE_KEY_B64` | Base64-encoded private key (PEM format) |

### Token Lifecycle

- Tokens are cached in memory and auto-refreshed **5 minutes before expiry**
- The `NetSuiteAuth` class handles all token management — no manual refresh needed
- Tokens are never persisted to disk

### Setup in NetSuite

1. Create an **Integration Record** in NetSuite (Setup → Integration → Manage Integrations → New)
2. Enable **OAuth 2.0** with the `restlets` and `rest_webservices` scopes
3. Generate a **public/private key pair** (RSA, 2048+ bits)
4. Upload the certificate to the integration record
5. Base64-encode the private key and set `NETSUITE_PRIVATE_KEY_B64`

## REST Client

`NetSuiteClient` wraps the REST API with:

- **Automatic retries** — 3 retries with exponential backoff (2s, 4s, 8s) for 502/503/504 errors
- **Pagination** — SuiteQL queries paginated at 1,000 records per page
- **Timeout** — 60-second default request timeout
- **Rate limiting** — `Prefer: transient` header to reduce server-side caching overhead

```python
from includes.netsuite import NetSuiteAuth, NetSuiteClient

client = NetSuiteClient()
# GET a record
resp = client.get("record/v1/vendor/12345")
# Run a SuiteQL query
resp = client.post("query/v1/suiteql", json={"q": "SELECT * FROM vendor LIMIT 10"})
```

## SuiteQL Queries

`includes/netsuite/queries.py` contains pre-built SuiteQL queries for each entity type. All queries use parameterized dates in NetSuite's `d/m/yyyy` format.

### Available Queries

| Function | Entity | Filter |
|---|---|---|
| `suppliers_updated_since(date)` | Vendors | Last modified date |
| `all_brands(since_date)` | Custom brand records | Optional date filter |
| `products_updated_since(date)` | Inventory items | Last modified date |
| `customers_updated_since(date)` | Customers | Last modified date |
| `contacts_updated_since(date)` | Contacts | Last modified date |
| `opportunities_updated_since(date)` | Opportunities | Last modified date |
| `quotes_updated_since(date)` | Quotes/Estimates | Last modified date |
| `sales_orders_updated_since(date)` | Sales orders | Last modified date |

## Sync Scripts

Each entity has a dedicated sync script in `scripts/`:

| Script | Entity | Default Range |
|---|---|---|
| `sync_netsuite_suppliers.py` | Vendors → `suppliers` table | Last 30 days |
| `sync_netsuite_brands.py` | Custom brands → `brands` table | Full sync |
| `sync_netsuite_products.py` | Inventory items → `products` table | Last 30 days |
| `sync_netsuite_customers.py` | Customers → `customers` table | Last 30 days |
| `sync_netsuite_contacts.py` | Contacts → `contacts` table | Last 30 days |
| `sync_netsuite_opportunities.py` | Opportunities → `opportunities` table | Last 30 days |
| `sync_netsuite_quotes.py` | Quotes → `transactions` table | Last 30 days |
| `sync_netsuite_sales_orders.py` | Sales orders → `transactions` table | Last 30 days |

### Running a Sync

```bash
# Sync suppliers from the last 30 days
uv run python -m scripts.sync_netsuite_suppliers

# Sync all brands (full history)
uv run python -m scripts.sync_netsuite_brands

# Sync products since a specific date
uv run python -m scripts.sync_netsuite_products --since 2026-01-01
```

### From the Chat UI

Admins can trigger syncs from the chat via the `SysAdminAgent` (when enabled). Scripts are registered in `config/scripts.py`:

```python
"sync_netsuite_suppliers": {
    "command": ["uv", "run", "python", "-m", "scripts.sync_netsuite_suppliers"],
    "description": "Sync suppliers from NetSuite API (default: last 30 days)",
    "args_allowed": ["--since"],
    "long_running": True,
}
```

## Nightly Sync

The `scripts/nightly_sync.py` script bundles all entity syncs and is designed to run on a schedule. It can be triggered via cron, Railway cron jobs, or manually.

## Deduplication

`includes/netsuite/sync_utils.py` handles deduplication:

- **By NetSuite ID** — Primary match key. If a record with the same `netsuite_id` exists, it's updated.
- **By name similarity** — For brands and suppliers, fuzzy matching is used to detect duplicates with different NetSuite IDs.
- **Currency resolution** — Supplier currencies are mapped to ISO codes and backfilled if missing.

## Data Flow

```
NetSuite API
  → NetSuiteClient.query()         [paginated SuiteQL]
  → sync_netsuite_*.py             [transform NetSuite fields → ORM fields]
  → sync_utils.deduplicate()       [find existing by netsuite_id]
  → SQLAlchemy session.merge()     [insert or update]
  → PostgreSQL
```

## Record Writes (Creating Items, Brands, Opportunities)

Write-backs to NetSuite use the REST record API (`record/v1/...`) through
`NetSuiteClient.create_record` / `update_record` / `get_record`, wrapped by
typed helpers in `includes/netsuite/records/`:

| Module | Helpers |
|---|---|
| `records/opportunity.py` | `create_opportunity`, `create_and_link_opportunity` — create Opportunities and link them to local RFQs |
| `records/item.py` | `find_item_by_part_number`, `find_brand_by_name`, `create_brand`, `get_or_create_brand`, `create_item`, `set_vendor_price`, `ensure_item_with_vendor` — inventory items + brands |

`ensure_item_with_vendor` is the high-level find-or-create flow: resolve brand
(create if missing), find an existing item (local smart product match, then a
NetSuite `itemid` lookup), then either refresh the vendor price or create the
item. Vendor and brand are mandatory — if either is missing no item is created.

### Item creation payload (verified live)

Minimal working `POST record/v1/inventoryitem`:

```json
{
  "itemId": "EGTEST-ITEM-001",
  "class": {"id": "1"},
  "salesDescription": "...",
  "purchaseDescription": "...",
  "department": {"id": "8"},
  "purchaseTaxCode": {"id": "15"},
  "salesTaxCode": {"id": "15"},
  "custitem_brand": {"id": "<customrecord_brands internal id>"},
  "itemVendor": {
    "items": [
      {"vendor": {"id": "<vendor internal id>"}, "preferredVendor": true, "purchasePrice": 123.45}
    ]
  },
  "externalId": "optional-idempotency-key"
}
```

Item vendor prices are stored in the **vendor's currency** — convert with
`includes.currency.convert(amount, from_iso, vendor_iso)` before sending.
The vendor record's `currency.refName` is the ISO code.

### Important REST behaviours (probed live)

- **`inventoryitem` has NO DELETE operation** — only get/put/post/patch
  (DELETE → 400 "There are no records of this type"). `opportunity` and
  `vendor` do support DELETE. For test hygiene use `externalId` upserts and
  distinctive prefixes; cleanup happens in the UI.
- **`itemVendor` sublist writes**: setting `purchasePrice` works when adding
  a line, but PATCH **ignores price changes on existing lines** (returns 204,
  nothing changes). The workaround mirrors the legacy Suitelet: clear the
  sublist (`PATCH ?replace=itemVendor` with `items: []`) then re-add all
  lines. `set_vendor_price` implements this and preserves other vendors'
  lines.
- GETs omit sublist content unless `?expandSubResources=true` is added.
- Valid PATCH query params are only `init`, `replace`, `replaceSelectedFields`.
- New NetSuite IDs are written back to local `products.netsuite_id` /
  `brands.netsuite_id` immediately after creation.

### SuiteQL lookups

```sql
SELECT id FROM item WHERE UPPER(itemid) = UPPER('...')              -- item existence
SELECT id FROM customrecord_brands WHERE UPPER(name) = UPPER('...') -- brand existence
SELECT purchaseprice FROM itemvendor WHERE item = '<item id>'       -- verify vendor price
```

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `401 Unauthorized` | Expired token or wrong credentials | Verify env vars, check certificate in NetSuite |
| `403 Forbidden` | Missing OAuth scopes | Add `rest_webservices` scope to integration record |
| `429 Too Many Requests` | Rate limited | Client auto-retries; reduce sync frequency |
| Empty results | No data modified in range | Use `--since` with an earlier date |
