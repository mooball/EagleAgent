## Plan: NetSuite REST API Integration

### Overview

Establish a production-ready integration layer between EagleAgent and NetSuite using the OAuth2 **client_credentials** grant with PS256-signed JWTs. The architecture uses a private key + certificate mapping (linked to entity "Tom Cameron") to acquire short-lived bearer tokens. All connections are traditional API calls triggered by code or cron — **not** agent-initiated.

Initial focus is **read-only** data retrieval, starting with supplier records.

**Long-term goals** (out of scope for this plan):
- Periodically pull updated supplier data from NetSuite
- Periodically pull new quotes and purchase records from NetSuite
- Periodically pull new product records from NetSuite
- Push updated supplier properties back to NetSuite

---

### Key Technical Details

- **Auth flow**: Sign a JWT with the private key (PS256 algorithm), exchange it at the token endpoint for a bearer token (~60 min TTL). No refresh tokens — just re-sign when expired.
- **Account ID**: `794882`
- **Token URL**: `https://794882.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token`
- **Private key**: Stored as base64-encoded environment variable `NETSUITE_PRIVATE_KEY_B64`. For local dev, the PEM file `netsuite_private_key_tom_cameron.pem` (project root, git-ignored) is base64-encoded into `.env`.
- **JWT header**: `alg=PS256`, `kid=<certificate_id>`, `typ=JWT`
- **JWT scope**: `["restlets", "rest_webservices"]`
- **Signing library**: PyJWT with `cryptography` backend
- **Working test script**: `scripts/test_netsuite_auth.py`

---

### Phase 1: Environment & Configuration ✅ DONE

1. **Add environment variables** to `config/settings.py`:
   - `NETSUITE_ACCOUNT_ID` — NetSuite account ID (default `794882`)
   - `NETSUITE_CLIENT_ID` — OAuth2 client ID (from env var, no default)
   - `NETSUITE_CERTIFICATE_ID` — Certificate ID / kid claim (from env var, no default)
   - `NETSUITE_PRIVATE_KEY_B64` — Base64-encoded PEM private key (from env var, no default)

2. **Encode the private key** for environment storage:
   ```bash
   base64 < netsuite_private_key_tom_cameron.pem | tr -d '\n'
   ```
   Paste the output into `.env` locally and into Railway's environment variables for production.

3. **Set Railway environment variables** for production:
   - `NETSUITE_CLIENT_ID`
   - `NETSUITE_CERTIFICATE_ID`
   - `NETSUITE_PRIVATE_KEY_B64`

3. **Verify dependencies** already present in `pyproject.toml`:
   - `PyJWT` with `cryptography` extras
   - `requests` (or `httpx` if preferred for async)

4. **Add `.gitignore` entries** for `netsuite_private_key*.pem` and `netsuite_public_cert*.pem` if not already present.

---

### Phase 2: NetSuite Client Module — `includes/netsuite/` ✅ DONE

Create a new `includes/netsuite/` package with the core integration code.

1. **`includes/netsuite/__init__.py`** — Package init, re-export client class.

2. **`includes/netsuite/auth.py`** — Token acquisition and caching:
   - `NetSuiteAuth` class that:
     - Decodes the base64 private key from `NETSUITE_PRIVATE_KEY_B64` env var on init
     - Signs a PS256 JWT with the correct claims (`iss`, `scope`, `aud`, `iat`, `exp`)
     - Exchanges the JWT for a bearer token via the token endpoint
     - Caches the token in memory with its expiry timestamp
     - Automatically refreshes when token is within 5 minutes of expiry
   - Expose a `get_token() -> str` method that returns a valid bearer token

3. **`includes/netsuite/client.py`** — HTTP client wrapper:
   - `NetSuiteClient` class that:
     - Takes a `NetSuiteAuth` instance
     - Provides `get()`, `post()` helper methods that auto-attach the `Authorization: Bearer` header
     - Builds base URLs from the account ID
     - Has a `suiteql(query: str) -> list[dict]` convenience method for running SuiteQL queries (handles pagination via `offset` / `limit`)
     - Has a `get_record(record_type: str, record_id: str) -> dict` convenience method
     - Handles HTTP errors and timeouts with clear error messages

4. **`includes/netsuite/queries.py`** — Reusable SuiteQL query definitions:
   - `suppliers_updated_since(date: str) -> str` — returns a SuiteQL query for vendor records modified after a given date
   - Future: similar functions for purchase orders, quotes, products

---

### Phase 3: Dashboard Health Check ✅ DONE

Add a NetSuite connection check to the System Admin dashboard.

1. **Add a "NetSuite Status" card** to the admin dashboard that:
   - Shows connection status (connected / error / not configured)
   - Shows the token expiry time if connected
   - Shows the NetSuite account ID
   - Runs a lightweight SuiteQL test query (e.g., `SELECT count(*) FROM vendor`)

2. **Add a dashboard endpoint** in `includes/dashboard/`:
   - `GET /admin/netsuite-status` — Returns JSON with connection status, token info, and a sample query result
   - Instantiates `NetSuiteClient`, attempts to get a token and run a test query

---

### Phase 4a: First Query — Fetch & Inspect Supplier Data

Develop and test the NetSuite vendor query, saving results to JSON for review.

1. **Create `scripts/fetch_netsuite_suppliers.py`**:
   - Accept `--since YYYY-MM-DD` argument (default: 30 days ago)
   - Use `NetSuiteClient` to run the `suppliers_updated_since()` query
   - Print results as a formatted table
   - Write results to `data/netsuite_vendors.json` for inspection

2. **SuiteQL query for vendors** (draft):
   ```sql
   SELECT id, entityId, companyName, email, phone, lastModifiedDate
   FROM vendor
   WHERE lastModifiedDate >= '2025-01-01'
   ORDER BY lastModifiedDate DESC
   ```

3. **Verify field mapping** — Run the query and inspect the JSON output to confirm which NetSuite vendor fields correspond to EagleAgent's `suppliers` table columns. Document the mapping.

---

### Phase 4b: Import & Merge Suppliers into Database

Once field mapping is confirmed, build the merge logic to upsert NetSuite vendors into the local `suppliers` table.

1. **Create `scripts/sync_netsuite_suppliers.py`**:
   - Accept `--since YYYY-MM-DD` argument (default: 30 days ago)
   - Fetch vendors from NetSuite using `NetSuiteClient`
   - For each vendor record:
     - Look up existing supplier by `netsuite_id` in the local database
     - **If exists**: merge/update fields that have changed (preserve any EagleAgent-only fields like embeddings, categories, etc.)
     - **If new**: insert a new supplier record with the NetSuite data mapped to local columns
   - Print a summary: created count, updated count, skipped/unchanged count

2. **Field merge strategy**:
   - NetSuite is the **source of truth** for core fields (name, contact info, status)
   - EagleAgent-only fields (embeddings, categories, internal notes) are never overwritten by the sync
   - Store the `netsuite_id` on the supplier record as the dedup/matching key

3. **`netsuite_id` column** already exists on the `suppliers` table — no migration needed.

4. **Add `state` column** to `suppliers` table (part of the address):
   - Create an Alembic migration to add `state` (String, nullable)

5. **Add `hubspot_id` column** to `suppliers` table:
   - Create an Alembic migration to add `hubspot_id` (String, nullable, unique)

---

### Phase 5: Brand Alignment

Re-import all brands from NetSuite and align them with the existing `brands` table using NetSuite IDs rather than name-matching (which was ~95% accurate).

1. **Query all brands from NetSuite**:
   - The `custentity_supplier_brand` field on vendors contains comma-separated brand IDs
   - Find the correct SuiteQL record/table for the brand list (likely a custom list)
   - https://794882.app.netsuite.com/app/common/custom/custrecordentrylist.nl?rectype=165
   - Pull all brand records with their ID and name

2. **Create `scripts/sync_netsuite_brands.py`**:
   - Fetch all brands from NetSuite
   - For each brand:
     - Look up existing brand in local DB by `netsuite_id`
     - **If exists**: update name if it has changed
     - **If new**: insert a new brand record with the NetSuite ID and name
     - **If local brand exists by name but has no `netsuite_id`**: backfill the `netsuite_id`
   - Print a summary: matched count, created count, updated count

3. **Verify alignment**:
   - Compare brand count in NetSuite vs local DB
   - Identify any orphaned local brands that don't have a NetSuite ID match

---

### Phase 6: Rebuild SupplierBrand Links

Rebuild the `supplier_brands` join table using the authoritative NetSuite vendor→brand links (from `custentity_supplier_brand`), replacing the original name-matched links.

1. **Create `scripts/sync_netsuite_supplier_brands.py`**:
   - Fetch all vendors from NetSuite (or a subset) with their `custentity_supplier_brand` field
   - For each vendor:
     - Look up the matching supplier in local DB by `netsuite_id`
     - Parse the comma-separated brand IDs from `custentity_supplier_brand`
     - For each brand ID:
       - Look up the local brand by `netsuite_id`
       - Create a `SupplierBrand` link if it doesn't already exist
     - Remove any existing `SupplierBrand` links for this supplier that are no longer in the NetSuite data
   - Print a summary: links created, links removed, suppliers processed

2. **Strategy**:
   - NetSuite is the source of truth for supplier↔brand relationships
   - The sync replaces the old name-matched links with ID-verified links
   - Suppliers without a `netsuite_id` in the local DB are skipped (they haven't been synced yet)

3. **Prerequisite**: Phase 4b (suppliers synced with `netsuite_id`) and Phase 5 (brands aligned with `netsuite_id`) must be completed first.

---

### Relevant Files

- `config/settings.py` — Add NetSuite configuration variables
- `includes/netsuite/__init__.py` — New package
- `includes/netsuite/auth.py` — Token management
- `includes/netsuite/client.py` — HTTP client wrapper
- `includes/netsuite/queries.py` — SuiteQL query definitions
- `includes/dashboard/` — Admin dashboard NetSuite status endpoint
- `scripts/test_netsuite_auth.py` — Existing working auth test
- `scripts/fetch_netsuite_suppliers.py` — New fetch-to-JSON script (Phase 4a)
- `scripts/sync_netsuite_suppliers.py` — New import/merge script (Phase 4b)
- `scripts/sync_netsuite_brands.py` — Brand alignment script (Phase 5)
- `scripts/sync_netsuite_supplier_brands.py` — Rebuild supplier↔brand links (Phase 6)

### Verification

1. Run `scripts/test_netsuite_auth.py` to confirm auth still works.
2. Run `scripts/fetch_netsuite_suppliers.py --since 2025-01-01` and verify vendor JSON is written to `data/`.
3. Review field mapping in the JSON output, confirm it matches the `suppliers` table schema.
4. Run `scripts/sync_netsuite_suppliers.py --since 2025-01-01` and verify records are created/updated in the local database.
5. Check the admin dashboard NetSuite status card shows "connected".
6. Confirm all NetSuite secrets are properly loaded from environment variables (not hardcoded), including the base64-decoded private key.
7. Run `scripts/sync_netsuite_brands.py` and verify all brands have `netsuite_id` populated.
8. Run `scripts/sync_netsuite_supplier_brands.py` and verify supplier↔brand links match NetSuite data.
