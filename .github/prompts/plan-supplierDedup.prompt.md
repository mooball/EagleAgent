# Plan: Supplier Deduplication — find → nominate → merge

> Status: **PROPOSAL — review before implementation.**
> Created: 2026-08-24
> Branch: `dedupe-suppliers`

## Goal

Rebuild supplier deduplication from the ground up:

1. **Reusable merge** — a library-level `merge_suppliers()` taking `primary` +
   `duplicate` + config options (`merge_contacts`, `merge_domains`,
   `merge_names`), that reassigns every reference to the duplicate and
   understands the web-vs-NetSuite distinction.
2. **Fast finder + nomination** — a much improved duplicate scanner (DB-side
   matching instead of Python O(N×M), **wide-net recall**) plus tools for users
   to nominate duplicates and merge them — **a human approves every merge**.
3. **Lookup hygiene** — everywhere a supplier is looked up, skip records with
   `use_instead` set, or resolve them through to the primary.

## Background & findings (2026-08-24)

Current state, found by code review:

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

## Phase 0 — Schema

Migration (single Alembic revision):

1. `Supplier.use_instead` — UUID, self-FK → `suppliers.id`, nullable, indexed.
   Same pointer direction as `Brand.duplicate_of`, but named for its consumer
   semantics: the flag lives on the *superseded* row and points at the record
   to use instead (decision 2026-08-24 — see "Decisions"). Note
   `scripts/sync_prod_data.py` already disables FK triggers for
   self-referencing tables.
2. New table `supplier_duplicate_nominations`:
   `id` UUID PK, `primary_id` / `duplicate_id` UUID FK (suppliers), `source`
   ('auto' | 'manual'), `status` ('proposed' | 'confirmed' | 'rejected' |
   'merged'), `reason` Text (nullable), `created_by` String, `created_at`,
   `merged_at` (nullable). Index on `status` and `(primary_id, duplicate_id)`.
3. `Supplier.name_norm` — String, nullable; normalised name for matching
   (Phase 1). GIN trigram index:
   `CREATE INDEX ix_suppliers_name_norm_trgm ON suppliers USING gin (name_norm gin_trgm_ops);`
   (a raw-name `lower(name)` index is unnecessary — matching runs on `name_norm`).
4. `Supplier.domain_keys` — JSONB, nullable; normalised domain keys (Phase 1).
   GIN index for `?` contains lookups:
   `CREATE INDEX ix_suppliers_domain_keys_gin ON suppliers USING gin (domain_keys);`

## Phase 1 — Normalisation & candidate engine

New module `includes/dashboard/supplier_matching.py` — the single engine used
by both the dedup scanner and everyday lookup (`match_supplier*`). Python
normalises at write time; matching itself stays **pure SQL** (pg_trgm + GIN).

### 1a. Name normalisation

`normalize_supplier_name(name) -> str`:

1. Unicode NFKC fold (smart quotes/dashes), lowercase.
2. `&` and all punctuation/special characters → space; collapse whitespace.
3. Tokenise on whitespace; drop `NOISE_TOKENS` — a **data-driven stoplist**
   seeded with: `and the of for pty ltd limited co company corp corporation
   inc incorporated llc plc group holdings international industries
   industrial australia australian au aust nz usa solutions supplies supply
   distribution imports exports` (+ findings from the analysis script below).
   Word order is preserved so trigrams still see ordering.
4. Keep alphanumerics (e.g. `3M` survives as `3m`).

Result is stored in `Supplier.name_norm` so trigram matching runs fully in
SQL; Python only executes on row writes.

**Data-scan script** `scripts/analyze_supplier_names.py` (read-only):

- Token-frequency report over existing supplier names — token, occurrence
  count, distinct-supplier share, example names — to grow `NOISE_TOKENS`
  deliberately ("scan existing data to find more ideas").
- `--dry-run` mode: re-run candidate matching with a proposed stoplist and
  list newly-matched pairs — cheap way to validate changes / tune thresholds.

### 1b. Domain keys (backup signal, free-mail excluded)

- `FREEMAIL_DOMAINS` set (gmail, googlemail, hotmail, outlook, live, msn,
  yahoo, ymail, icloud, me, mac, aol, proton, bigpond, optusnet, tpg,
  iinet, ...). Free-mail domains never produce domain candidates.
- `domain_key(url) -> str | None`: existing `_extract_domain()` (registrable
  domain, ccTLD-aware), then strip the TLD(s) → `abcparts.com.au` →
  `abcparts`. Same-SLD-across-TLD pairs now surface (`abc.com` vs
  `abc.com.au`) — treat as a *wider* candidate source, always corroborated
  by name similarity (same SLD under different TLDs can be different
  businesses).
- Union of `url` + `alt_domains` + contact-URL keys stored in
  `Supplier.domain_keys` (JSONB, GIN-indexed) → SQL: `WHERE domain_keys ? :k`.

### 1c. Write-time derivation + backfill

- SQLAlchemy `before_insert` / `before_update` events on `Supplier`
  (registered from `supplier_matching.py`, NOT `models.py`, to avoid the
  circular import) so every ORM write path — `sync_netsuite_suppliers.py`
  upserts, web-adds, dashboard edits — keeps `name_norm` and `domain_keys`
  fresh.
- `scripts/backfill_supplier_norms.py` for existing rows (batched,
  re-runnable, `--dry-run` — same pattern as
  `backfill_product_departments.py`).

### 1d. Candidate queries (index-backed)

`find_supplier_candidates(session, name, domain=None, limit=10)`:

1. *Domain candidates* — `Supplier.domain_keys.has_key(dk)` (GIN) when the
   incoming domain has a non-free-mail key. Small, high-precision set.
2. *Name candidates* — `Supplier.name_norm % :q` (GIN trigram operator;
   threshold via `pg_trgm.set_limit()` at pool init), ordered by
   `similarity(name_norm, :q) DESC`, top-k only.
3. Union + dedup, `.filter(Supplier.use_instead.is_(None))`, return rows with
   reason tags for downstream confidence scoring.

`match_supplier_by_name()` / `match_supplier()` are re-implemented on top of
this helper, so RFQ-time lookup and dedup scanning finally share one engine
(today they are separate implementations).

**Two consumers, two policies** (decision 2026-08-24) — the candidate helper
is only the *retrieval* layer; decision policy lives in the consumers:

- **Lookup** (`match_supplier*`): high precision — an automated decision
  ("is this web-found supplier already in the DB?") that must be certain.
- **Dedup finder** (Phase 3): high recall — cast a wide net, surface every
  reason, and let a **human decide** before any merge.

**Tests**: normaliser units (punctuation, `&`, stopwords, unicode, `3M`);
`domain_key` units (ccTLDs, free-mail exclusion); candidate-query tests;
backfill idempotency.

## Phase 2 — Reusable `merge_suppliers()` library

New module `includes/dashboard/supplier_dedup.py` (imported by admin routes,
chat tools, scripts). Replaces `merge_supplier()` in
`scripts/find_duplicate_suppliers.py` (thin wrapper kept for admin route until
routes are migrated).

Merging is **always human-triggered** (admin UI or nomination) — the finder
never merges automatically.

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

def merge_suppliers(session, primary_id, duplicate_id, config=MergeConfig()) -> MergeResult:
    ...
```

Behaviour:

- **Always** reassign references from duplicate → primary:
  - `SupplierBrand` (delete conflicting pairs, same as today)
  - `RFQItem.suppliers` and `RFQItem.brand_suppliers` JSONB (`supplier_id` key,
    refresh `name` from primary) — *via SQL/JSONB where possible, not a
    full-table Python scan*
  - `EmailTracking`, `Contact`, `Transaction` bulk `UPDATE ... SET supplier_id`
  - `RFQ.supplier_meta` (JSONB keyed by supplier name) — see Open Questions
- **`merge_contacts`**: merge `contacts` JSONB (dedup by url+email) — existing
  logic; `Contact` rows always reassign.
- **`merge_domains`**: move duplicate's `url` + `alt_domains` (+ contact URLs)
  into primary's `alt_domains`; only take duplicate's `url` if primary has none.
- **`merge_names`**: append duplicate's `name` + `alt_names` into primary's
  `alt_names` (case-insensitive dedup).
- **Web vs NetSuite matrix:**

  | primary | duplicate | action |
  |---|---|---|
  | netsuite | web | merge + delete duplicate (today's behaviour) |
  | web | web | merge + delete duplicate |
  | netsuite | netsuite | **keep both records** — reassign local references to primary, set `duplicate.use_instead = primary.id` |
  | web | netsuite | reject with error ("swap primary/duplicate") |

- Idempotent: merge of an already-merged/deleted duplicate returns a clean
  error result, no crash.
- Caller commits; the function never commits itself (matches existing pattern).

**Tests** (`tests/test_supplier_dedup.py`): every matrix cell; each config
flag on/off; each reference table reassigned; JSONB name refreshed;
use_instead set + record kept; error cases.

## Phase 3 — Fast finder + nomination tools

### 3a. Finder rewrite (`scripts/find_duplicate_suppliers.py`)

**High-recall mode**: cast a wide net over the whole table and let a human
review the results — candidates may be fuzzy and not exact, that's expected.
Rebuild the scan on the Phase-1 candidate engine — no O(N×M) Python loops:

1. **Exact-normalised dupes first** — one SQL pass over non-NetSuite rows:
   `GROUP BY name_norm HAVING count(*) > 1` — unambiguous candidates.
2. **Per-supplier candidates** — for each remaining non-NetSuite supplier,
   call `find_supplier_candidates()` against NetSuite suppliers (domain-key
   pass + trigram pass, top-k each). Index-backed; never load all rows.
3. **Internal scan** — same per-supplier candidates against other
   non-NetSuite rows, plus a domain-key overlap query
   (`s1.domain_keys && s2.domain_keys`, GIN).
4. **Refine in Python** only on small candidate sets: token-Jaccard +
   SequenceMatcher on `name_norm`, containment, country/domain agreement;
   keep confidence + reasons per pair.
5. **Thresholds** — surface matches above ~0.6 with reasons; no hard 0.9
   gate. Domain-key matches get high confidence but are always name-checked.
6. **(Optional) Embeddings** — `name_embedding` (Gemini, reuse notes
   plumbing) into the existing `Vector(256)` column, pgvector top-k as an
   extra candidate block; one-off backfill like
   `backfill_product_departments.py`.

### 3b. Scan-as-job + nominations UI

- **Background scan**: run the finder via `job_runner` (job_runner pattern
  already exists) instead of a synchronous POST; persist auto candidates into
  `supplier_duplicate_nominations` (`source='auto'`). The admin page lists them
  — no more HTTP timeouts.
- **Nominate manually**: on the supplier detail/list pages, a "Nominate
  duplicate" action — typeahead search for the primary, saves a
  `source='manual'` nomination.
- **Admin page** (`/admin/duplicates`): two sections — *Auto candidates* and
  *Nominations* — each card shows both suppliers, confidence/reasons, and
  Merge / Reject buttons. Merge calls the Phase-2 merge library with the three
  config checkboxes. Reject marks the nomination `rejected`.
- Keep `/admin/duplicates/scan` working as a quick "scan now" (runs finder
  inline for small result limits) so admins aren't forced to wait for the job.

## Phase 4 — Lookup updates (`use_instead` hygiene)

**Resolver-first, not scattered filters.** Brands lesson (verified 2026-08-24):
`Brand.duplicate_of` is filtered on in ~11 scattered call sites and *never*
resolved — products pointing at a marked duplicate keep pointing at it. For
suppliers, avoid repeating this:

- Helper in `supplier_dedup.py`: `resolve_supplier_id(session, id)` — follows
  `use_instead` chains (cycle guard, max hops) to the primary.
- Optional convenience query helpers in the same module, e.g.
  `active_suppliers(session)` (a `.filter(Supplier.use_instead.is_(None))`
  base query) so call sites reuse one filter instead of each hand-rolling it.
- `match_supplier_by_name()` / `match_supplier()`
  (`includes/dashboard/database.py`): re-implemented on the Phase-1
  `find_supplier_candidates()` engine; flagged rows are filtered out of every
  pass and resolved at the end if the only match is a flagged row. **Keep
  the existing verification policy** (domain + country agreement;
  containment-only acceptance for weak matches) — precision matters here
  because lookup drives automated decisions, unlike the wide-net finder.
- **Choice lists** (where a user picks a supplier): use the shared filter —
  supplier list route, `_find_suppliers_by_brand`, `rfq_crud` supplier search
  helpers, `supplier_categorization` queries.
- **Display paths** (history, transactions): resolve through
  `resolve_supplier_id()` so RFQ items referencing a flagged duplicate render
  the primary name.
- **Sync guard**: `scripts/sync_netsuite_suppliers.py` upsert must not clobber
  `use_instead` (and web-sourced dedup logic there should honour it).

`use_instead` is the **safety net**, not the primary mechanism: Phase-2 merge
reassignment rewrites references at merge time; the flag catches anything
missed and redirects future lookups.

**Tests**: extend `tests/test_database_matching.py` — flagged suppliers are
skipped; chained `use_instead` resolves to the final primary; cycle safe.

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
- **Normalisation strategy** (2026-08-24) — Python normalisation at write
  time into stored `name_norm` / `domain_keys` columns; matching stays pure
  SQL (pg_trgm `%` with GIN, JSONB `?` for domain keys). Stopword list is
  data-driven via `scripts/analyze_supplier_names.py`; free-mail domains are
  excluded from domain matching.

## Open questions (need decisions)

1. **Both NetSuite**: when primary and duplicate are both in NetSuite, should
   local references (RFQ items, transactions) also be reassigned to primary, or
   only `use_instead` set? *(Proposed: reassign + flag.)*
2. **`RFQ.supplier_meta`** is keyed by supplier *name* — remap keys during
   merge? *(Proposed: yes, remap keys matching duplicate's name.)*
3. **Undo** — is a merge expected to be undoable? *(Proposed: no full undo;
   nomination rows record before/after IDs for manual recovery.)*
4. **Embeddings** — worth the one-off backfill now, or keep trigram+domain
   first and add embeddings later if recall is poor? *(Proposed: ship trigram+
   domain first; embeddings as follow-up.)*
5. Old `scripts/find_duplicate_suppliers.py` — keep as wrapper or delete?
   *(Proposed: delete once routes/tools import the new module.)*

## Notes / conventions

- Follow existing patterns: `Brand.duplicate_of` for the self-FK shape (but
  named `use_instead` per decision), `deduplicate_brands.py` for interactive
  review UX, `job_runner` for background scans, `flag_modified` for JSONB
  column updates.
- Run `uv run pytest tests/ -x --timeout=60` after each phase; matching tests
  live in `tests/test_database_matching.py`.
