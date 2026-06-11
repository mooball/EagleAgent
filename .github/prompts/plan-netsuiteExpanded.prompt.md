# Plan: NetSuite Expanded Integration – Opportunities, Customers, & Team Members

## Status: Complete ✅ (All Phases Delivered)

## Immediate Next Steps

All implementation phases are complete. Remaining future work:

1. **Phase 6: RFQ Linking & Email Matching** — Add `opportunity_id`/`customer_id` to RFQs, implement email-to-contact matching logic

## Context

We have successfully integrated NetSuite suppliers, products, brands, and quotes. The next phase expands this to support:

1. **Opportunities** — Sales opportunities/pipelines in NetSuite
2. **Customers** — Company and individual customer records
3. **Contacts** — Expanded from NetSuite's `contactlist` field on customers
4. **Team Members** — Employees/sales reps for linking to opportunities and customers

These are foundational for:
- **RFQ Linking** — Link RFQs to NetSuite opportunities and customers
- **Email Matching** — Parse incoming emails and match to customer contacts by email/domain
- **Sales Pipeline** — Visibility into customer opportunities and sales rep assignments
- **Contact Management** — Maintain a local, searchable contact database for email resolution

## Key Technical Details

### NetSuite Data Structure

- **Opportunities** stored in `opportunity` table (accessible via SuiteQL)
- **Customers** stored in `customer` table
- **Contacts** stored in `contact` table; also referenced via `contactlist` on customer records (comma-separated IDs)
- **Employees** — **No dedicated table**. Employees are:
  - Referenced via `employee` field in transactions
  - Accessible via `BUILTIN.DF(employee)` to get display name
  - Include system users (e.g., `-5` for Bill Watt) and regular employees

### Employee Mapping Challenge

Since NetSuite has no standalone employee table, we must:
1. Extract unique employees from transactions
2. Create a local mapping table linking NetSuite employee IDs to local user IDs
3. Match by email/name (one-time manual setup)
4. Reference this mapping when syncing opportunities and customers

### Discovered Employees (17 total)

From transaction history analysis, with Google Workspace email matching:

#### Active Employees (10 total)

| NetSuite ID | Name | Email | Status |
|------------|------|-------|--------|
| -5 | Bill Watt | bill@eagle-exports.com | Active (Admin) |
| 8145 | Darren Whiting | darren@eagle-exports.com | Active |
| 9768 | Tomoko Matsuba | tomoko@eagle-exports.com | Active |
| 17625 | Annabelle Watt | annabelle@eagle-exports.com | Active |
| 32463 | Angie Bonus | angie@eagle-exports.com | Active |
| 33331 | Harry Busacay | harry@eagle-exports.com | Active |
| 524093 | Sandy Smith | sandy@eagle-exports.com | Active |
| 1886802 | Matt Davis | matt@eagle-exports.com | Active |
| 7401040 | Bernard Saw | bernard@eagle-exports.com | Active |
| 7965937 | Harry Watt | hwatt@eagle-exports.com | Active |

#### Inactive / No Direct Account (7 total)

| NetSuite ID | Name | Status |
|------------|------|--------|
| 406 | Guillaume Amiot | No Google Workspace account |
| 16966 | Jarred Parkinson | No Google Workspace account |
| 30631 | Robert Cuddeford | No Google Workspace account |
| 33431 | Myles Garner | No Google Workspace account |
| 35472 | Nathan Eacott | No Google Workspace account |
| 2847756 | Fletcher Watt | No Google Workspace account |
| 7300978 | Daniel Lakay | No Google Workspace account |

---

## Phase 1: Database Models

> **Conventions** (matching existing models):
> - Primary key: `id = Column(UUID)`
> - NetSuite sync key: `netsuite_id = Column(String, unique=True)` — stores the NetSuite internal integer `id` as a string, used for upsert matching
> - Sync timestamp: `netsuite_last_modified = Column(DateTime(timezone=True))` — stores NetSuite's `lastmodifieddate`, used for incremental sync queries
> - Cross-table references: store both the raw NetSuite ID (String) **and** the resolved local UUID FK, so records can be inserted before their related records are synced

---

### 1. `opportunities` table

Maps to NetSuite's `opportunity` record type. From recon, a sample record has:
`id="13"`, `tranid="OP1009"`, `title="Fire Doors"`, `entity="1147"`, `salesrep="-5"`, `status="C"`, `total="0"`, `currency="1"`

```python
class Opportunity(Base):
    __tablename__ = 'opportunities'

    id                   = Column(UUID,    primary_key=True, default=uuid.uuid4)
    netsuite_id          = Column(String,  unique=True, nullable=False, index=True)
                           # ← NetSuite field: id (e.g. "13")

    opportunity_number   = Column(String,  nullable=True)
                           # ← NetSuite field: tranid (e.g. "OP1009") — human-facing reference

    title                = Column(String,  nullable=True)
                           # ← NetSuite field: title (e.g. "Fire Doors")

    status               = Column(String,  nullable=True)
                           # ← NetSuite field: status (e.g. "C" = Closed, "O" = Open)

    total                = Column(Float,   nullable=True)
                           # ← NetSuite field: total

    currency             = Column(String,  nullable=True)
                           # ← NetSuite field: currency (raw ID e.g. "1"; use BUILTIN.DF if needed)

    # --- Customer link ---
    netsuite_customer_id = Column(String,  nullable=True, index=True)
                           # ← NetSuite field: entity (e.g. "1147") — raw NS customer ID
                           #   stored so this record can be inserted before customers are synced

    customer_id          = Column(UUID,    ForeignKey('customers.id'), nullable=True, index=True)
                           # ← resolved local FK; populated by matching netsuite_customer_id
                           #   → customers.netsuite_id

    # --- Sales rep link ---
    netsuite_salesrep_id = Column(String,  nullable=True)
                           # ← NetSuite field: salesrep (e.g. "-5") — raw NS employee ID

    salesrep_id          = Column(Integer, ForeignKey('netsuite_employee_mappings.id'), nullable=True)
                           # ← resolved local FK; populated by matching netsuite_salesrep_id
                           #   → netsuite_employee_mappings.netsuite_employee_id

    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
                           # ← NetSuite field: lastmodifieddate — used for incremental sync
```

---

### 2. `customers` table

Maps to NetSuite's `customer` record type. From recon, a sample record has:
`id="4"`, `entityid="Eagle Exports"`, `companyname="Eagle Exports"`, `email="bill@eagle-exports.com"`, `entitystatus="10"`, `salesrep="-5"`

```python
class Customer(Base):
    __tablename__ = 'customers'

    id                   = Column(UUID,    primary_key=True, default=uuid.uuid4)
    netsuite_id          = Column(String,  unique=True, nullable=False, index=True)
                           # ← NetSuite field: id (e.g. "4")

    entity_code          = Column(String,  nullable=True)
                           # ← NetSuite field: entityid — a short code or display label NS assigns
                           #   (often same as companyname, but not always)

    companyname          = Column(String,  nullable=True)
                           # ← NetSuite field: companyname (nullable for individual customers)

    fullname             = Column(String,  nullable=True)
                           # ← NetSuite field: fullname (used for individual customers/contacts)

    email                = Column(String,  nullable=True, index=True)
                           # ← NetSuite field: email — top-level company email

    phone                = Column(String,  nullable=True)
                           # ← NetSuite field: phone

    isinactive           = Column(Boolean, nullable=False, default=False)
                           # ← NetSuite field: isinactive ("T"/"F" → True/False)

    currency             = Column(String,  nullable=True)
                           # ← NetSuite field: currency (raw ID)

    # --- Sales rep link ---
    netsuite_salesrep_id = Column(String,  nullable=True)
                           # ← NetSuite field: salesrep (e.g. "-5") — raw NS employee ID

    salesrep_id          = Column(Integer, ForeignKey('netsuite_employee_mappings.id'), nullable=True)
                           # ← resolved local FK

    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
                           # ← NetSuite field: lastmodifieddate
```

---

### 3. `contacts` table

Unified contact table covering **both supplier and customer contacts**. Supplier contacts come
from vendor record fields (no NS contact record → no `netsuite_id`). Customer contacts come from
NetSuite's `contact` record type and do have a `netsuite_id`.

```python
class Contact(Base):
    __tablename__ = 'contacts'

    id                   = Column(UUID,    primary_key=True, default=uuid.uuid4)
    netsuite_id          = Column(String,  unique=True, nullable=True, index=True)
                           # ← NetSuite field: id from contact table
                           #   NULL for supplier contacts (they come from vendor fields, not a NS contact record)

    # --- Parent link (exactly one must be set) ---
    supplier_id          = Column(UUID,    ForeignKey('suppliers.id'),  nullable=True, index=True)
    customer_id          = Column(UUID,    ForeignKey('customers.id'),  nullable=True, index=True)

    label                = Column(String,  nullable=True)
                           # ← Used for supplier contacts only: "Main", "Source", "Source CC"
                           #   NULL for customer contacts (they have firstname/lastname instead)

    firstname            = Column(String,  nullable=True)
                           # ← NetSuite field: firstname (customer contacts)

    lastname             = Column(String,  nullable=True)
                           # ← NetSuite field: lastname (customer contacts)

    fullname             = Column(String,  nullable=True)
                           # ← NetSuite field: fullname — also used as display name for supplier contacts

    email                = Column(String,  nullable=True, index=True)
                           # ← Primary matching field for email inbox parsing

    phone                = Column(String,  nullable=True)

    isinactive           = Column(Boolean, nullable=False, default=False)
                           # ← NetSuite field: isinactive ("T"/"F" → True/False)
                           #   Always False for supplier contacts (set manually if needed)

    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
                           # ← NetSuite field: lastmodifieddate (NULL for supplier contacts)
```

---

### 4. `netsuite_employee_mappings` table

Manual mapping table linking NetSuite employee IDs to local users (no NS employee table exists in SuiteQL).

```python
class NetSuiteEmployeeMapping(Base):
    __tablename__ = 'netsuite_employee_mappings'

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    netsuite_employee_id   = Column(String,  unique=True, nullable=False)
                             # ← Raw NS employee ID (e.g. "-5", "9768")

    name                   = Column(String,  nullable=False)
                             # ← Display name from BUILTIN.DF(employee) (e.g. "Bill Watt")

    email                  = Column(String,  nullable=True, index=True)
                             # ← Google Workspace email — used to link to local user

    is_active              = Column(Boolean, nullable=False, default=True)
                             # ← False for former staff with no Google Workspace account
```

### 5. Create Alembic migration
   - Add all four tables to the database schema

---

## Phase 2: SuiteQL Query Builders

### 1. Add query functions to `includes/netsuite/queries.py`

   ```python
   def opportunities_updated_since(since_date: str) -> str:
       # SELECT id, tranid, title, entity, status, salesrep, total, currency, lastmodifieddate
       # WHERE lastmodifieddate >= <date>
   
   def customers_updated_since(since_date: str) -> str:
       # SELECT id, entityid, companyname, fullname, email, phone, isinactive, currency, salesrep, contactlist, lastmodifieddate
       # WHERE isinactive = 'F' AND lastmodifieddate >= <date>
   
   def contacts_for_ids(contact_ids: list[str]) -> str:
       # SELECT id, firstname, lastname, email, phone, company, isinactive, lastmodifieddate
       # FROM contact WHERE id IN (<ids>)
   
   def contacts_updated_since(since_date: str) -> str:
       # SELECT id, firstname, lastname, email, phone, company, isinactive, lastmodifieddate
       # FROM contact WHERE lastmodifieddate >= <date>
   ```

---

## Phase 3: Sync Scripts

### 1. Create `scripts/sync_netsuite_opportunities.py`
   - Usage: `uv run python -m scripts.sync_netsuite_opportunities [--since "7 days"] [--dry-run] [--resume]`
   - Logic:
     - Query opportunities using SuiteQL
     - For each: upsert by `netsuite_id`
     - Validate `entity_id` (customer) exists before linking
     - Validate `salesrep_id` (employee) exists or has a mapping
   - Output: Progress, counts of inserted/updated/skipped records

### 2. Create `scripts/sync_netsuite_customers.py`
   - Usage: `uv run python -m scripts.sync_netsuite_customers [--since "7 days"] [--dry-run] [--resume]`
   - Logic:
     - Query customers using SuiteQL
     - For each: upsert by `netsuite_id`
     - Parse `contactlist` (comma-separated contact IDs)
     - Trigger contact sync for those contact IDs
     - Validate `salesrep_id` (employee) has a mapping
   - Output: Progress, counts of inserted/updated/skipped records

### 3. Create `scripts/sync_netsuite_contacts.py`
   - Usage: `uv run python -m scripts.sync_netsuite_contacts [--customer-ids "1,2,3"] [--dry-run]`
   - Logic:
     - Query contacts by customer ID or in bulk
     - For each: upsert by `netsuite_id`
     - Extract email and phone for later matching
   - Output: Progress, counts of inserted/updated/skipped records

### 4. Create `scripts/list_netsuite_employees.py`
   - Usage: `uv run python -m scripts.list_netsuite_employees [--export mapping.csv]`
   - Logic:
     - Extract unique employees from NetSuite transactions
     - Display employee ID and name
     - Option to export to CSV for manual matching
     - Show local users for reference
   - Output: Formatted list or CSV file

---

## Phase 4: Employee Mapping Setup

### 1. Populate `netsuite_employee_mappings` table with verified mappings

The following mappings have been verified via Google Workspace directory match:

```python
EMPLOYEE_MAPPINGS = [
    {"netsuite_id": -5, "name": "Bill Watt", "email": "bill@eagle-exports.com"},
    {"netsuite_id": 8145, "name": "Darren Whiting", "email": "darren@eagle-exports.com"},
    {"netsuite_id": 9768, "name": "Tomoko Matsuba", "email": "tomoko@eagle-exports.com"},
    {"netsuite_id": 17625, "name": "Annabelle Watt", "email": "annabelle@eagle-exports.com"},
    {"netsuite_id": 32463, "name": "Angie Bonus", "email": "angie@eagle-exports.com"},
    {"netsuite_id": 33331, "name": "Harry Busacay", "email": "harry@eagle-exports.com"},
    {"netsuite_id": 524093, "name": "Sandy Smith", "email": "sandy@eagle-exports.com"},
    {"netsuite_id": 1886802, "name": "Matt Davis", "email": "matt@eagle-exports.com"},
    {"netsuite_id": 7401040, "name": "Bernard Saw", "email": "bernard@eagle-exports.com"},
    {"netsuite_id": 7965937, "name": "Harry Watt", "email": "hwatt@eagle-exports.com"},
]

# Unmapped (no direct Google Workspace account):
# - 406 | Guillaume Amiot
# - 16966 | Jarred Parkinson
# - 30631 | Robert Cuddeford
# - 33431 | Myles Garner
# - 35472 | Nathan Eacott
# - 2847756 | Fletcher Watt
# - 7300978 | Daniel Lakay
```

### 2. Create mapping script
   - Script: `scripts/create_netsuite_employee_mappings.py`
   - Inserts verified mappings into `netsuite_employee_mappings` table
   - Logs unmapped employees for manual review
   - Can be run once or as part of database initialization

### 3. Add to `scripts/nightly_sync.py` ✅
   - Contacts, customers, opportunities added to nightly sync (steps 6-8)
   - Nightly sync order: Brands → Products → Suppliers → Quotes → Sales Orders → Contacts → Customers → Opportunities → Link Brands → Categorize → Notes → Embeddings

### 4. Create admin UI (future phase)
   - Web form to add/update employee mappings
   - Match employees by Google Workspace lookup
   - Allow manual entry for employees without Google accounts

---

## Phase 5: Integration & Testing

### 1. Migrate supplier contacts from JSONB → `contacts` table ✅
   - Script: `scripts/migrate_supplier_contacts.py` created and run
   - Reads existing `suppliers.contacts` JSONB and inserts into `contacts` table
   - Preserves `label` field ("Main", "Source", "Source CC")

### 2. Update `scripts/sync_netsuite_suppliers.py` ✅
   - `build_contacts()` now writes to `contacts` table via `sync_supplier_contacts()`
   - Upserts contacts by `supplier_id` + `label` (no `netsuite_id` for supplier contacts)
   - Marks stale contacts as inactive

### 3. Update `scripts/nightly_sync.py` ✅
   - Added contacts, customers, opportunities sync jobs (steps 6-8)
   - All scripts use `--resume` for incremental sync

### 4. Add to `includes/dashboard/models.py` ✅
   - Relationships added: `Opportunity.customer`, `Opportunity.salesrep`, `Opportunity.transactions`, `Customer.customer_contacts`, `Customer.opportunities`, `Supplier.supplier_contacts`
   - Indexes on all FK columns

### 5. Dashboard routes ✅
   - Created `includes/dashboard/routes/opportunities.py` (list, detail, search)
   - Created `includes/dashboard/routes/customers.py` (list, detail, search)
   - Created `includes/dashboard/routes/contacts.py` (list, detail, search)

### 6. End-to-end testing ✅
   - Full sync run against live NetSuite data in production
   - Record counts and data integrity verified
   - Opportunity totals and status codes validated

---

## Phase 6: RFQ Linking & Email Matching (Future)

### 1. Add RFQ columns
   - `opportunity_id` (FK to Opportunity)
   - `customer_id` (FK to Customer)

### 2. Email matching logic
   Because all contacts (suppliers and customers) are in one table, a single query resolves any incoming email:

   ```sql
   -- Step 1: exact email match
   SELECT c.*, s.name as supplier_name, cu.companyname as customer_name
   FROM contacts c
   LEFT JOIN suppliers s ON c.supplier_id = s.id
   LEFT JOIN customers cu ON c.customer_id = cu.id
   WHERE c.email = 'sender@example.com'

   -- Step 2 (fallback): domain match
   WHERE c.email LIKE '%@example.com'
   ```

   Resolution priority:
   1. Exact email match → link directly to contact's parent (supplier or customer)
   2. Domain match → list all contacts at that domain for disambiguation
   3. No match → flag for manual assignment

---

## Files to Create/Modify

| File | Action | Status | Purpose |
|------|--------|--------|---------|
| `includes/dashboard/models.py` | Modify | ✅ Complete | Add Opportunity, Customer, Contact (unified), EmployeeMapping models; add `Supplier.contacts` relationship |
| `alembic/versions/e8f2a1b7c4d3_add_netsuite_expanded_tables.py` | Create | ✅ Complete | Migration to add four new tables (not yet applied) |
| `includes/netsuite/queries.py` | Modify | ✅ Complete | Add query builders for opportunities, customers, contacts (all 4 builders added) |
| `scripts/sync_netsuite_opportunities.py` | Create | ✅ Complete | Sync opportunities from NetSuite with --since/--resume/--dry-run |
| `scripts/sync_netsuite_customers.py` | Create | ✅ Complete | Sync customers and trigger contact sync with --since/--resume/--dry-run |
| `scripts/sync_netsuite_contacts.py` | Create | ✅ Complete | Sync contacts from NetSuite with --since/--resume/--dry-run/--customer-ids |
| `scripts/migrate_supplier_contacts.py` | Create | ✅ Complete | One-time migration of supplier JSONB contacts into unified contacts table |
| `scripts/create_netsuite_employee_mappings.py` | Create | ✅ Complete | Populate employee mappings table with 10 active + 7 inactive employees |
| `scripts/list_netsuite_employees.py` | Create | ✅ Complete | List employees with --export flag for CSV output |
| `scripts/sync_netsuite_suppliers.py` | Modify | ✅ Complete | Updated to write contacts to unified table via `sync_supplier_contacts()` |
| `scripts/nightly_sync.py` | Modify | ✅ Complete | Added contacts, customers, opportunities sync jobs (12 steps total) |
| `includes/dashboard/routes/opportunities.py` | Create | ✅ Complete | Dashboard list/detail/search routes for opportunities |
| `includes/dashboard/routes/customers.py` | Create | ✅ Complete | Dashboard list/detail/search routes for customers |
| `includes/dashboard/routes/contacts.py` | Create | ✅ Complete | Dashboard list/detail/search routes for contacts |
| `includes/netsuite/constants.py` | Modify | ✅ Complete | Added OPPORTUNITY_STATUS mapping (A-D codes) |
| `scripts/backfill_transaction_opportunities.py` | Create | ✅ Complete | Link existing transactions to opportunities by tranid prefix |
| `.github/prompts/plan-netsuiteExpanded.prompt.md` | Create | This plan document |

---

## Acceptance Criteria

### Phase 1-3: Complete ✅
- ✅ All four models created (Opportunity, Customer, Contact, NetSuiteEmployeeMapping)
- ✅ Alembic migration applied (e8f2a1b7c4d3 + f1a2b3c4d5e6 at head)
- ✅ SuiteQL query builders added (opportunities_updated_since, customers_updated_since, contacts_for_ids, contacts_updated_since)
- ✅ Opportunities sync script complete with --since/--resume/--dry-run and batch commits
- ✅ Customers sync script complete with contact expansion and contact sync trigger
- ✅ Contacts sync script complete with --customer-ids support
- ✅ Employee mapping script created with 10 active + 7 inactive employees
- ✅ Employee listing script with CSV export capability
- ✅ Supplier contacts migration script created
- ✅ Date parsing fixed (NetSuite d/m/yyyy → ISO datetime)
- ✅ Pagination fixed (all sync scripts use suiteql_iter with batch commits)
- ✅ `--resume` flag working on opportunities, customers, contacts

### Phase 4: Complete ✅
- ✅ Alembic migration applied (`uv run alembic upgrade head`)
- ✅ Employee mappings populated (`uv run python -m scripts.create_netsuite_employee_mappings`)
- ✅ Sync scripts tested against live NetSuite data (production deployment)

### Phase 5: Complete ✅
- ✅ Supplier contacts migrated to unified contacts table
- ✅ sync_netsuite_suppliers.py updated to write contacts to unified table
- ✅ nightly_sync.py updated with 12 steps including new sync jobs
- ✅ Relationships added to models.py (Opportunity.customer, Customer.contacts, Supplier.contacts)
- ✅ Dashboard routes created for opportunities, customers, contacts
- ✅ End-to-end sync verified in production
- ✅ Opportunity status codes mapped (A=In Progress, B=Issued Quote, C=Closed Won, D=Closed Lost)
- ✅ Transaction → Opportunity backfill script created

### Phase 6: Future 🔮
- ⏳ Integration tests for all new sync scripts
- ⏳ RFQ models updated with opportunity/customer FKs
- ⏳ Email matching logic implementation
- ⏳ Admin UI for employee mapping management

---

## Notes

- **NetSuite API limits:** SuiteQL pagination max 1000 records/page (handled by client)
- **Date format:** NetSuite uses `d/m/yyyy` internally; convert to ISO format for DB storage
- **Contact expansion:** May be slow if customers have many contacts; consider batching for large datasets
- **Employee mapping:** One-time setup; will need maintenance as new employees added to NetSuite
- **Incremental sync:** All scripts use `lastmodifieddate` for resumability
- **Data quality:** Filter inactive records where appropriate; validate FKs before linking

---

## Rollout Plan

1. **Week 1:** Phases 1-3 (models + queries + sync scripts) ✅ **COMPLETE**
   - All database models created and migration file generated
   - All SuiteQL query builders implemented
   - All 3 sync scripts created with proper pagination, batching, and logging
   - Employee mapping and listing scripts created
   - Supplier contacts migration script created

2. **Week 2:** Phase 4 (Database setup + testing)
   - Apply Alembic migration to create tables
   - Populate employee mappings
   - Run dry-runs of all sync scripts to verify data
   - Test end-to-end with real NetSuite data

3. **Week 3:** Phase 5 (Integration + supplier contacts)
   - Run supplier contacts migration
   - Update sync_netsuite_suppliers.py to write to unified contacts table
   - Update nightly_sync.py with new sync jobs
   - Add model relationships for queries

4. **Week 4:** Testing & Deployment
   - Integration tests for all sync scripts
   - Validate foreign key constraints
   - Performance testing with large datasets
   - Deploy to staging, then production

5. **Future:** Phase 6 (RFQ linking + email matching)
   - Add RFQ columns for opportunity/customer links
   - Implement email matching logic
   - Create admin UI for employee management
