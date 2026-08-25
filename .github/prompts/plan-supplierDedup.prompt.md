# Plan: Supplier Deduplication — find → nominate → merge

> Status: **PROPOSAL — review before implementation.**
> Created: 2026-08-24 · Restructured into two tracks 2026-08-25
> Branch: `dedupe-suppliers` (Track A) — Track B follows in its own branch

## Goal

Rebuild supplier deduplication as **two tracks over one shared library**:

- **Track A — clean up the existing mess** *(build now)*: a repeatable sweep of
  the ~9,940 NetSuite supplier records, reviewed and confirmed by the client
  through a simple web UI. Mostly **NS↔NS**; one-off, then re-run periodically.
- **Track B — filter all incoming data** *(build next, same libraries)*: stop
  new duplicates at the moment a web-sourced supplier is created. Almost
  entirely **Web↔NS**; ongoing, needs fast in-flow decisions.

Track A ships first because the mess exists today. **Track B is the higher
long-term priority**, so every library Track A builds is designed for Track B
to consume unchanged — see "Shared foundation" below.

Three deliverables, all shared:

1. **Normalisation + candidate engine** — one indexed way to ask "what looks
   like this supplier?"
2. **Reusable merge** — `merge_suppliers(primary, duplicate, config)` with
   `merge_contacts` / `merge_domains` / `merge_names`, that reassigns every
   reference and understands the web-vs-NetSuite distinction.
3. **Lookup hygiene** — `use_instead` respected/resolved everywhere.

## Data snapshot (measured 2026-08-25, local DB)

| Metric | Value |
|---|---|
| Total suppliers | 10,115 |
| `source='netsuite'` | **9,940 (98.3%)** |
| `source='web'` | **175 (1.7%)** |
| Web rows that later got a `netsuite_id` | 0 |
| Exact-normalised duplicate groups | **85** (floor — crude normalisation, exact match only) |

Almost every group found is **NetSuite ↔ NetSuite**:

```
[2 netsuite] Airport Lighting Specialists   | Airport Lighting Specialists Pty Ltd
[2 netsuite] Autex Australia Pty Ltd        | Autex Pty Ltd
[2 netsuite] B J Inns Pty Ltd               | B.J. INNS PTY LTD.
[2 netsuite] Billiard Shop                  | The Billiard Shop
[2 netsuite] A. C. M. LABORATORY PTY. LTD.  | A.C.M. LABORATORY PTY LTD
```

Pair-count scale: web×NS = 1.74M (today's timeout); **NS×NS = 49.4M**
— impossible in Python, routine for a GIN trigram index.

## Background & findings (2026-08-24)

Current state, found by code review:

- **⚠️ The current scanner cannot see the real duplicates.**
  `scan_duplicates()` compares web→NetSuite; `scan_internal_duplicates()`
  compares web→web. **Neither ever compares NetSuite against NetSuite**, so
  the ~85 NS↔NS groups are structurally invisible — a bigger cause of "rarely
  finds matches" than the 0.9 threshold. Track A must scan **all pairs**.
- **Scan** (`scripts/find_duplicate_suppliers.py`):
  - `scan_duplicates()` / `scan_internal_duplicates()` load **all** suppliers
    into Python and run an O(N×M) pairwise loop using
    `difflib.SequenceMatcher` (`_name_similarity`). This is why `/admin/duplicates/scan`
    times out.
  - Only candidates above **0.9** confidence are returned — most real matches
    are dropped.
  - `pg_trgm` is already installed (`alembic/versions/ddbd16aabcbf_add_pg_trgm_extension.py`)
    and `match_supplier_by_name()` already uses `func.similarity` in SQL, but
    the scanner doesn't.
- **Merge** (`merge_supplier()` in the same script, called from
  `admin.py` route `/admin/duplicates/merge`):
  - Already reassigns `SupplierBrand`, `RFQItem.suppliers` JSONB,
    `RFQItem.brand_suppliers` JSONB, `EmailTracking.supplier_id`,
    `Contact.supplier_id`, `Transaction.supplier_id`, merges `url`/`alt_domains`
    and `contacts`.
  - Lives in a script — not reusable by other tools.
  - Unconditionally `session.delete()`s the duplicate — no web-vs-NetSuite
    distinction, no `use_instead` flagging.
  - No "merge names" option (alt-names merging happens implicitly, untestable).
  - Full-table scan of all `RFQItem` rows in Python for JSONB updates.
- **Model** (`includes/dashboard/models.py`):
  - `Supplier` has no `use_instead`; `Brand.duplicate_of` (self-FK UUID) is the
    existing precedent for this pattern.
  - `Supplier.embedding Vector(256)` already exists (used for notes) and
    pgvector is available — usable for name-embedding matching.
- **Lookup sites that don't know about dedup today:**
  `match_supplier_by_name()` / `match_supplier()` in
  `includes/dashboard/database.py`, supplier list route
  (`includes/dashboard/routes/suppliers.py`), `_find_suppliers_by_brand()`
  (`includes/tools/product_tools.py`), RFQ supplier tools
  (`includes/tools/rfq_crud.py`, `supplier_search_tools.py`), categorization
  queries, and dashboard joins `Transaction → Supplier`.
- **The Track-B leak point** — `includes/tools/quote_tools.py` (~L448–480):
  `match_supplier()` runs; if it returns None the code **silently creates a
  new `source='web'` supplier**. `match_supplier()` already computes
  near-misses and their rejection reasons, then throws them away into a log
  line (`"...but REJECTED: domain mismatch..."`). That discarded information
  is exactly what Track B needs.
- **"Dismissed" is currently a data hack** — marking a supplier not-a-duplicate
  appends the magic string `__dedup_reviewed__` into its `alt_names` JSONB
  array (`admin.py` L371, read back in `find_duplicate_suppliers.py` L70/222).
  The candidates table replaces this; a cleanup step must strip the marker.

## Two tracks at a glance

| | **Track A — cleanup** | **Track B — filter** |
|---|---|---|
| Scope | 9,940 NS records, all pairs | 1 incoming vs 9,940 |
| Mix | ~100% NS↔NS | ~99% Web↔NS |
| Cadence | One-off + periodic re-run | Every web supplier creation |
| Query shape | Batch self-join | Single indexed top-k |
| Merge outcome | `use_instead` flag (NS can't be deleted) | Delete web row — or never create it |
| UI | Batch review page (client-operated) | In-flow prompt (reuses A's queue at first) |
| Ships | Now | Next branch |

# Shared foundation (built in Track A, consumed unchanged by Track B)

## S0 — Schema

One Alembic revision. Current head is `w8x9y0z1a2b3`, so:

```python
# alembic/versions/x9y0z1a2b3c4_add_supplier_dedup.py
revision: str = 'x9y0z1a2b3c4'
down_revision: Union[str, None] = 'w8x9y0z1a2b3'

def upgrade() -> None:
    # 1. use_instead: points from the superseded row to the one to use
    op.add_column('suppliers',
        sa.Column('use_instead', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key('fk_suppliers_use_instead', 'suppliers', 'suppliers',
                          ['use_instead'], ['id'])
    op.create_index('ix_suppliers_use_instead', 'suppliers', ['use_instead'])

    # 2. One row per searchable key (name variant or domain) per supplier
    op.create_table(
        'supplier_match_keys',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('supplier_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('suppliers.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('key_type', sa.String(10), nullable=False),   # 'name' | 'domain'
        sa.Column('key_value', sa.String(), nullable=False),
        sa.UniqueConstraint('supplier_id', 'key_type', 'key_value',
                            name='uq_supplier_match_key'),
    )
    op.create_index('ix_smk_type_value', 'supplier_match_keys',
                    ['key_type', 'key_value'])          # exact domain lookups
    op.execute("""
        CREATE INDEX ix_smk_name_trgm ON supplier_match_keys
        USING gin (key_value gin_trgm_ops) WHERE key_type = 'name';
    """)                                                 # fuzzy name lookups

    # 3. Review queue — auto-scan results and manual nominations
    op.create_table(
        'supplier_duplicate_candidates',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('primary_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('duplicate_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source', sa.String(10), nullable=False),      # 'auto' | 'manual'
        sa.Column('status', sa.String(10), nullable=False,
                  server_default='proposed'),                    # proposed|merged|rejected
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('reasons', postgresql.JSONB(), nullable=True),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.String(), nullable=True),
        sa.UniqueConstraint('primary_id', 'duplicate_id', name='uq_dup_candidate_pair'),
    )
    op.create_index('ix_sdc_status', 'supplier_duplicate_candidates', ['status'])
```

**Decisions baked in (no ambiguity):**

- **One `supplier_match_keys` table, not columns on `suppliers`.** Alt-names and
  multiple domains are naturally multi-valued; one row per key makes both the
  batch self-join and the single-row lookup trivial, and a partial GIN index
  keeps the trigram index name-only. No `name_norm`/`domain_keys` columns are
  added to `suppliers`.
- **Statuses are `proposed | merged | rejected`** — no separate `confirmed`;
  confirming *is* merging.
- **`ondelete='CASCADE'`** on both FKs so deleting a merged web supplier cleans
  up its keys and candidate rows automatically.

## S1 — Normalisation & candidate engine

New module `includes/dashboard/supplier_matching.py`. Python normalises on
write; all matching is **pure SQL** against the indexed keys table.

### S1a. Name normalisation

```python
import re, unicodedata

NOISE_TOKENS = {
    "and", "the", "of", "for",
    "pty", "ltd", "limited", "co", "company", "inc", "incorporated",
    "corp", "corporation", "llc", "plc", "pl", "gmbh", "bv", "nv", "srl",
    "group", "holdings", "international", "intl",
    "australia", "australian", "aust", "au",
}
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

def normalize_supplier_name(name: str) -> str:
    """'A.C.M. Laboratory Pty. Ltd.' -> 'acm laboratory'"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = _PUNCT_RE.sub(" ", s.lower())
    tokens = [t for t in s.split() if t not in NOISE_TOKENS]
    if not tokens:            # name was ALL noise (e.g. "The Company") — keep it
        tokens = s.split()
    return " ".join(tokens)
```

The all-noise fallback matters: without it `"The Co"` normalises to `""` and
would false-match every other empty key.

**Stoplist tuning** — `scripts/analyze_supplier_names.py` (read-only): token
frequency + distinct-supplier share + examples, so tokens like `au`/`aust` are
confirmed safe before being added. A `--compare` mode re-runs pair matching
with a proposed stoplist and prints newly-matched pairs.

### S1b. Domain keys (free-mail excluded, TLD stripped)

```python
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.com.au", "ymail.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "bigpond.com",
    "bigpond.net.au", "optusnet.com.au", "tpg.com.au", "iinet.net.au",
}

def domain_key(value: str | None) -> str | None:
    """'https://www.abcparts.com.au/x' -> 'abcparts'. None if free-mail."""
    if not value:
        return None
    raw = value if "//" in value else f"http://{value}"
    d = _extract_domain(raw)                    # existing ccTLD-aware helper
    if not d or d in FREEMAIL_DOMAINS:
        return None
    return d.split(".")[0] or None
```

Stripping the TLD makes `abc.com` and `abc.com.au` share a key — deliberately
wider, and always corroborated by name similarity before it counts.

### S1c. Building keys (explicit calls + backfill safety net)

```python
def supplier_match_keys(sup) -> list[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for n in [sup.name, *(sup.alt_names or [])]:
        if n and n.startswith("__"):        # legacy __dedup_reviewed__ marker
            continue
        k = normalize_supplier_name(n or "")
        if k:
            keys.add(("name", k))
    for u in [sup.url, *(sup.alt_domains or [])]:
        if k := domain_key(u):
            keys.add(("domain", k))
    for c in (sup.contacts or []):
        if not isinstance(c, dict):
            continue
        if k := domain_key(c.get("url")):
            keys.add(("domain", k))
        email = c.get("email") or ""
        if "@" in email and (k := domain_key(email.rsplit("@", 1)[-1])):
            keys.add(("domain", k))
    return sorted(keys)


def rebuild_match_keys(session, sup) -> None:
    """Idempotent — safe to call on every supplier write."""
    session.query(SupplierMatchKey).filter_by(supplier_id=sup.id).delete()
    for kt, kv in supplier_match_keys(sup):
        session.add(SupplierMatchKey(supplier_id=sup.id, key_type=kt, key_value=kv))
```

**Call it explicitly at the four known write points** — chosen over ORM event
listeners because it is predictable, testable, and greppable:

| Where | Line(s) |
|---|---|
| `scripts/sync_netsuite_suppliers.py` | update ~L323/L354, insert ~L366 |
| `includes/tools/quote_tools.py` | new web supplier ~L469 |
| `includes/dashboard/database.py` | `update_supplier()` |
| `supplier_dedup.merge_suppliers()` | after names/domains merge |

Safety net: `scripts/backfill_supplier_match_keys.py` (batched, re-runnable,
`--dry-run`) rebuilds everything and reports drift — needed anyway for the
initial populate and after raw-SQL paths like `sync_prod_data.py`.

### S1d. Candidate lookup (single supplier — Track B + `match_supplier*`)

Two small indexed queries, merged in Python (clearer than one clever query):

```python
_NAME_SQL = text("""
    SELECT s.id, s.name, s.netsuite_id, max(similarity(k.key_value, :nk)) AS sim
    FROM supplier_match_keys k JOIN suppliers s ON s.id = k.supplier_id
    WHERE k.key_type = 'name' AND k.key_value %% :nk AND s.use_instead IS NULL
    GROUP BY s.id, s.name, s.netsuite_id
    ORDER BY sim DESC
    LIMIT :limit
""")

_DOMAIN_SQL = text("""
    SELECT DISTINCT s.id, s.name, s.netsuite_id
    FROM supplier_match_keys k JOIN suppliers s ON s.id = k.supplier_id
    WHERE k.key_type = 'domain' AND k.key_value = :dk AND s.use_instead IS NULL
""")

def find_supplier_candidates(session, name, url=None, limit=10) -> list[dict]:
    """Retrieval only — no accept/reject policy. Returns rows + reason tags."""
    session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.4"))
    ...
```

**`SET LOCAL`, not `set_limit()` at pool init.** The `%` operator reads the
session GUC `pg_trgm.similarity_threshold`; with `pool_size=10,
max_overflow=20` plus recycling and `pool_pre_ping`, a pool-init value is
silently lost. `SET LOCAL` scopes it to the current transaction. (Note `%%`
escaping inside SQLAlchemy `text()`.)

**Retrieval vs policy** — `find_supplier_candidates()` only *retrieves*.
Decision policy belongs to each consumer:

- **Track A finder**: high recall, wide net, a human decides.
- **Track B / `match_supplier*`**: high precision, automated decision.
  Normalisation may only *generate* candidates — the accept/reject step must
  still compare **original names + domain + country**. Stripping `australia`
  makes `Autex Australia Pty Ltd` ≡ `Autex Pty Ltd` (a true positive here) but
  would equally collapse `Repco Australia` ≡ `Repco`; never let normalisation
  alone confirm a link.

**Tests**: normaliser units (punctuation, `&`, all-noise fallback, unicode,
`3M`); `domain_key` (ccTLDs, free-mail, bare domains, email addresses);
`rebuild_match_keys` idempotency; candidate queries against seeded rows.

## S2 — Reusable `merge_suppliers()` library

New module `includes/dashboard/supplier_dedup.py` (imported by admin routes,
chat tools, scripts). `merge_supplier()` in
`scripts/find_duplicate_suppliers.py` is deleted once the admin route moves
over.

Merging is **always human-triggered** — no scan ever merges automatically.

```python
@dataclass
class MergeConfig:
    merge_contacts: bool = True
    merge_domains: bool = True
    merge_names: bool = True

@dataclass
class MergeResult:
    primary_id: UUID
    duplicate_id: UUID
    deleted: bool                 # False when duplicate kept (NetSuite)
    use_instead_set: bool
    counts: dict[str, int]        # per-table reassignment counts
    warnings: list[str]

def merge_suppliers(session, primary_id, duplicate_id,
                    config=MergeConfig()) -> MergeResult:
    ...
```

Behaviour:

- **Always** reassign references from duplicate → primary:
  - `SupplierBrand` (delete conflicting pairs, same as today)
  - `RFQItem.suppliers` and `RFQItem.brand_suppliers` JSONB (`supplier_id` key,
    refresh `name` from primary) — *via SQL/JSONB where possible, not a
    full-table Python scan*
  - `EmailTracking`, `Contact`, `Transaction` bulk `UPDATE ... SET supplier_id`
  - `RFQ.supplier_meta` (JSONB keyed by supplier *name*) — remap keys matching
    the duplicate's name
- **`merge_contacts`**: merge `contacts` JSONB (dedup by url+email) — existing
  logic; `Contact` rows always reassign.
- **`merge_domains`**: move duplicate's `url` + `alt_domains` (+ contact URLs)
  into primary's `alt_domains`; only take duplicate's `url` if primary has none.
- **`merge_names`**: append duplicate's `name` + `alt_names` into primary's
  `alt_names` (case-insensitive dedup).
- **Web vs NetSuite matrix** — for Track A the **netsuite+netsuite row is the
  normal case**, not an edge case:

  | primary | duplicate | action |
  |---|---|---|
  | netsuite | netsuite | **keep both rows** — reassign all local references to primary, set `duplicate.use_instead = primary.id` (Track A default) |
  | netsuite | web | merge + delete duplicate (Track B default) |
  | web | web | merge + delete duplicate |
  | web | netsuite | reject with error ("swap primary/duplicate") |

- After any merge, call `rebuild_match_keys()` on the primary (names/domains
  changed) and on the duplicate if it survives.
- Idempotent: merging an already-merged/deleted duplicate returns a clean
  error result, no crash.
- Caller commits; the function never commits itself (matches existing pattern).

**Tests** (`tests/test_supplier_dedup.py`): every matrix cell; each config
flag on/off; each reference table reassigned; JSONB name refreshed;
use_instead set + record kept; keys rebuilt; error cases.

## S3 — Lookup hygiene (`use_instead`)

**Resolver-first, not scattered filters.** Brands lesson (verified 2026-08-24):
`Brand.duplicate_of` is filtered on in ~11 scattered call sites and *never*
resolved — products pointing at a marked duplicate keep pointing at it. 1,912
brands are flagged, so this is a live gap. Don't repeat it:

```python
def resolve_supplier_id(session, supplier_id, _max_hops=5) -> UUID:
    """Follow use_instead to the surviving primary. Cycle-safe."""
    seen = set()
    while supplier_id and supplier_id not in seen and _max_hops:
        seen.add(supplier_id)
        nxt = session.query(Supplier.use_instead).filter(
            Supplier.id == supplier_id).scalar()
        if not nxt:
            break
        supplier_id, _max_hops = nxt, _max_hops - 1
    return supplier_id

def active_suppliers(session):
    """Base query for any user-facing supplier choice list."""
    return session.query(Supplier).filter(Supplier.use_instead.is_(None))
```

- **Choice lists** use `active_suppliers()`: supplier list route,
  `_find_suppliers_by_brand`, `rfq_crud` supplier helpers,
  `supplier_categorization` queries.
- **Display paths** (history, transactions) resolve via `resolve_supplier_id()`
  so rows pointing at a flagged duplicate render the primary.
- **`match_supplier*`** already filter `use_instead IS NULL` inside
  `find_supplier_candidates()`; keep their existing verification policy
  (domain + country agreement, containment-only acceptance).
- **Sync guard**: `sync_netsuite_suppliers.py` must never clobber
  `use_instead` on update.

`use_instead` is the **safety net**, not the primary mechanism: S2 rewrites
references at merge time; the flag catches anything missed and redirects
future lookups.

**Tests**: extend `tests/test_database_matching.py` — flagged suppliers skipped;
chained `use_instead` resolves to the final primary; cycle safe.

---

# Track A — clean up the existing mess (build now)

## A1 — All-pairs scan (`scripts/scan_supplier_duplicates.py`)

Replaces `scripts/find_duplicate_suppliers.py`. Scans **all suppliers against
all suppliers** — no web/NetSuite split. Two set-based queries, not 10k
round-trips:

```sql
-- run once per scan, inside one transaction
SET LOCAL pg_trgm.similarity_threshold = 0.45;

-- (1) fuzzy name pairs — uses ix_smk_name_trgm
SELECT a.supplier_id AS id_a, b.supplier_id AS id_b,
       max(similarity(a.key_value, b.key_value)) AS sim
FROM supplier_match_keys a
JOIN supplier_match_keys b
  ON a.supplier_id < b.supplier_id        -- each pair once
 AND a.key_type = 'name' AND b.key_type = 'name'
 AND a.key_value % b.key_value
GROUP BY a.supplier_id, b.supplier_id;

-- (2) shared-domain pairs — uses ix_smk_type_value
SELECT a.supplier_id AS id_a, b.supplier_id AS id_b, a.key_value AS domain_key
FROM supplier_match_keys a
JOIN supplier_match_keys b
  ON a.supplier_id < b.supplier_id
 AND a.key_type = 'domain' AND b.key_type = 'domain'
 AND a.key_value = b.key_value;
```

Then, in Python over the (small) pair set only:

1. Score: max name similarity, token-Jaccard, containment, shared domain,
   country agreement → `confidence` + `reasons[]`.
2. Assign a review tier:
   - **`certain`** — identical normalised name, or shared domain **and**
     sim ≥ 0.8. Eligible for bulk-confirm in the UI.
   - **`review`** — everything else above ~0.6.
3. Pick primary/duplicate with the existing `_pick_keep_remove()` scoring
   (netsuite_id ≫ contacts ≫ url), preserved from the old script.
4. Upsert into `supplier_duplicate_candidates` (unique on the pair), **skipping
   pairs already `merged`/`rejected`** so re-runs don't resurface decisions.

Run it as a registered background script:

```python
# config/scripts.py
"scan_supplier_duplicates": {
    "command": ["uv", "run", "python", "-m", "scripts.scan_supplier_duplicates"],
    "description": "Scan all suppliers for duplicate candidates (writes review queue)",
    "args_allowed": ["--min-confidence", "--dry-run", "--report"],
    "long_running": True,
},
```

### Threshold tuning (deliberately empirical)

The cutoffs **cannot be chosen up front** — pick them by looking at real
results. Build `--report` first and use it before any rows are written:

- Scan at a deliberately loose trigram floor (0.3), write nothing, and print a
  **confidence histogram** — bucket counts plus ~5 sample pairs per bucket:

  ```
  0.95-1.00   62 pairs   Airport Lighting Specialists | ... Pty Ltd
  0.85-0.95   48 pairs   Autex Australia Pty Ltd      | Autex Pty Ltd
  0.75-0.85   ...
  0.65-0.75   ...        <- eyeball where true positives stop
  ```

- Read down the buckets, find where false positives start dominating, and set
  `--min-confidence` there for the real run.
- Re-check after any `NOISE_TOKENS` change (`analyze_supplier_names.py
  --compare` shows which pairs a stoplist edit newly creates).

Starting point only, expected to move: trigram floor 0.45, reported confidence
0.6. The `certain` tier (identical normalised name, or shared domain + sim ≥
0.8) should be validated the same way before bulk-confirm is enabled.

## A2 — Review UI (client-operated, deliberately plain)

The client runs this, so it must be web-based — but it only needs to scan and
confirm. Reuse the existing card styling in
`templates/partials/_admin_dedup_results.html`; no new design work.

```
GET  /admin/duplicates                    page shell (filters + Scan button)
POST /admin/duplicates/scan               -> job_runner.run_script(...)  [202, no wait]
GET  /partial/admin/duplicates/list       paginated queue (status=proposed)
POST /admin/duplicates/{id}/merge         -> merge_suppliers(...)  [returns swapped card]
POST /admin/duplicates/{id}/reject        -> status='rejected'
POST /admin/duplicates/bulk-merge         -> merge every checked id
```

UI requirements, in priority order:

1. **Throughput over polish** — paginated list (50/page), each row showing both
   names, source badges, confidence, reasons, and Merge / Not-a-duplicate.
2. **Bulk-confirm the `certain` tier** — a filtered view with checkboxes and
   "merge all checked". This clears the bulk of the 85 groups in one pass.
3. **Merge options** — three checkboxes (`contacts`, `domains`, `names`)
   defaulting to on, mapped straight to `MergeConfig`.
4. **Scan progress** — poll the existing `/partial/admin/jobs` panel rather
   than building anything new.
5. **Manual nomination** — on the supplier detail page, "mark as duplicate
   of…" writes a `source='manual'` row. (Lower priority than 1–3.)

## A3 — Data cleanup

- Strip the legacy `__dedup_reviewed__` marker from `alt_names`, converting
  each into a `rejected` candidate row where the pair is known (otherwise just
  drop the marker). One-off, inside the backfill script.
- Run `backfill_supplier_match_keys.py`, then the scan, then review.

---

# Track B — filter incoming data (next branch)

Sketched now so the shared libraries fit it; **not built in this branch**.

## B1 — Stop discarding near-misses

`match_supplier()` currently returns `Supplier | None` and logs its rejection
reasons. Change it to return the reasoning:

```python
@dataclass
class SupplierMatch:
    supplier: Supplier | None          # confident match, or None
    near_misses: list[dict]            # [{supplier, confidence, reasons, rejected_because}]
```

Existing callers keep working via `.supplier`; the near-misses become the
input to the human decision.

## B2 — Close the leak at creation

At `quote_tools.py` ~L469, when no confident match exists **but near-misses
do**: still create the web supplier (never block the agent pipeline), and
write a `source='auto'` row into `supplier_duplicate_candidates` pointing at
the best near-miss.

That means **Track B needs no new UI on day one** — the Track A review queue
surfaces it, and merging deletes the web row (matrix row 2). An inline
"link to existing / confirm new" prompt in the RFQ supplier flow is the
follow-up refinement.

## B3 — Ongoing quality loop

Schedule `scan_supplier_duplicates` periodically (new NS vendors keep arriving
via sync). The two loops compound: cleanup raises lookup precision, precise
lookup slows duplicate regrowth.

## Decisions (settled)

- **`use_instead` naming** (2026-08-24) — field points from the superseded
  record to the preferred one, same direction as `Brand.duplicate_of`. Chosen
  because NetSuite duplicates can never be deleted, so the durable state is
  "this row stays, use that one"; `use_instead` also reads naturally in
  resolvers (`while sup.use_instead: ...`). `Brand.duplicate_of` may be
  renamed to match later — trivial rename + migration, out of scope here.
- **Lookup hygiene is resolver-first** — one `resolve_supplier_id()` helper
  plus shared filtered-query helpers, not per-site `.filter()` calls.
- **Two-tier matching strategy** (2026-08-24) — dedup finding and supplier
  lookup are deliberately different policies over the same normalised engine:

  | | Dedup finder | Lookup matching |
  |---|---|---|
  | Question | "is this possibly a duplicate?" | "is this supplier already in the DB?" |
  | Goal | high recall, wide net | high precision, certainty |
  | Threshold | loose (~0.6, reasons shown) | strict (existing verification) |
  | Decides | human, before any merge | automated (agent/tool) |
  | Cost of error | low (human filters false positives) | high (wrong link) |

  Merges are never automatic — always human-triggered. Over time the two
  loops compound: dedup cleaning raises lookup precision, precise lookup
  reduces new duplicates entering the system.
- **Normalisation strategy** (2026-08-24) — Python normalisation on write into
  the `supplier_match_keys` table; matching stays pure SQL (pg_trgm `%` with a
  partial GIN index, btree equality for domains). Stopword list is data-driven
  via `scripts/analyze_supplier_names.py`; free-mail domains excluded.
- **Two tracks, one library** (2026-08-25) — Track A (NS↔NS cleanup) ships
  first because the mess exists now; Track B (Web↔NS filter) is the higher
  long-term priority and reuses S0–S3 unchanged. Track A's review queue is
  also Track B's day-one UI.
- **Track A gets a web UI** (2026-08-25) — a CLI would be the natural fit for
  a one-off, but the **client** runs this, so it must be browser-based. Plain
  is fine: reuse existing card markup, prioritise throughput and bulk-confirm
  over design.
- **All-pairs scanning** (2026-08-25) — the scan must compare every supplier
  against every other, not web→NetSuite. Two set-based self-joins over
  `supplier_match_keys`, never a per-supplier query loop.
- **Explicit `rebuild_match_keys()` calls**, not ORM event listeners —
  predictable and greppable, with the backfill script as the drift safety net.
- **Embeddings are cut from scope** — 10k suppliers and ~85 groups; trigram +
  domain is sufficient. Revisit only if recall proves poor.

## Resolved questions

1. **Both NetSuite** — reassign local references to the primary **and** set
   `use_instead`. This is Track A's normal path, not an edge case.
2. **`RFQ.supplier_meta`** (keyed by supplier *name*) — remap keys matching the
   duplicate's name during merge.
3. **Undo** — no full undo. `supplier_duplicate_candidates` retains both IDs,
   the decision, and who made it, which is enough for manual recovery.
4. **Old `scripts/find_duplicate_suppliers.py`** — delete once
   `scan_supplier_duplicates.py` and `supplier_dedup.py` land; keep
   `_pick_keep_remove()` scoring by porting it across.
5. **NetSuite-side remediation** (2026-08-25) — **deferred**. Flagged NS↔NS
   pairs stay duplicated in NetSuite for now; local `use_instead` flagging is
   sufficient. When a NetSuite cleanup does happen, the candidates table is
   already the source list — export it, or drive vendor merges via the
   NetSuite API. Nothing in this design needs to change to enable that, so no
   work now.
6. **Scan threshold** (2026-08-25) — **tune empirically, not up front**. Ship
   `--report` (confidence histogram + sample pairs), run it against real data,
   then pick the cutoff. See "Threshold tuning" under A1.

## Open questions

None outstanding — ready to build once the plan is approved.

## Build order

1. S0 migration + models
2. S1 `supplier_matching.py` + backfill script + tests
3. S2 `supplier_dedup.py` (`merge_suppliers`) + tests
4. A1 scan script (+ registry entry), **`--report` first** — tune thresholds
   against real data before writing any candidate rows
5. A2 admin UI (list → merge → reject → bulk-confirm, in that order)
6. A3 cleanup + first live run with the client
7. S3 lookup hygiene
8. → new branch for Track B

## Notes / conventions

- Follow existing patterns: `Brand.duplicate_of` for the self-FK shape (but
  named `use_instead` per decision), `deduplicate_brands.py` for interactive
  review UX, `job_runner` for background scans, `flag_modified` for JSONB
  column updates.
- Run `uv run pytest tests/ -x --timeout=60` after each phase; matching tests
  live in `tests/test_database_matching.py`.
