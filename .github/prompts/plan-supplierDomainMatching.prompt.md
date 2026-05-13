# Plan: Improved Supplier Domain Matching

## Problem

When a supplier changes their domain name (or uses multiple domains), the current matching logic rejects what is clearly the same business and creates a duplicate supplier record.

**Example:**
- DB has "John Barnes" with domain `johnbarnesqld.com.au`
- Agent finds "John Barnes Group" at `johnbarnesgroup.au`
- Both domains redirect to the same site — same business
- Current logic: name matches via containment, but domain mismatch → REJECTED → duplicate created

The domain check at `includes/dashboard/database.py` lines 188-196 treats any domain mismatch as an absolute rejection, which is too strict for real-world supplier data.

## Current Matching Flow

```
match_supplier() in includes/dashboard/database.py:

1. Domain-first lookup: scan all suppliers for exact domain match → return immediately
2. Name-based match (trigram/containment via match_supplier_by_name)
3. Verification gates (all-or-nothing):
   a. Domain check: if both have domains, they MUST match exactly → reject on mismatch
   b. Country check: if both have countries, they MUST match → reject on mismatch
   c. No-attributes check: if neither domain nor country, only accept containment matches
```

The domain-first lookup (`_extract_domain`) strips subdomains but requires exact root domain match. There is no concept of domain aliases, redirects, or fuzzy domain comparison.

## Proposed Changes

### 1. Multi-Domain Support for Suppliers

Add an `aliases` or `domains` field to the Supplier model to store multiple known domains for a single supplier.

**Schema change:**
- Add `domains` column (JSON array of strings) to the `suppliers` table, e.g. `["johnbarnesgroup.au", "johnbarnesqld.com.au"]`
- Or simpler: add a `domain_aliases` column alongside existing `url` field
- Alembic migration to backfill from existing `url` values

**Domain-first lookup update:**
- When scanning suppliers, check incoming domain against ALL domains in the `domains` list, not just the primary `url`
- When a new domain is discovered for an existing supplier (via redirect or match), auto-append it to the `domains` list

### 2. Hybrid Domain Matching (Relaxed Rejection)

When name match is strong but domains differ, apply graduated logic instead of hard rejection:

```
If name-match is containment AND domains share a business-name root:
  → Accept match, add new domain to supplier's aliases, log as domain migration
If name-match is containment BUT domains share nothing:
  → Reject (likely different business)
If name-match is trigram-only:
  → Keep current strict behavior (reject on domain mismatch)
```

**Business-name root extraction:**
- Strip the TLD (`.com.au`, `.au`, `.co.nz`, etc.)
- Strip common business suffixes: `group`, `qld`, `nsw`, `vic`, `au`, `australia`, `nz`, `wholesale`, `export`, `online`, `shop`, `store`, `sales`, `parts`, `supplies`
- Compare remaining stems — if one contains the other (e.g. `johnbarnes` in both), it's likely the same business

**Implementation in `_extract_domain_stem()`:**
```python
def _extract_domain_stem(url: str) -> str | None:
    """Extract the core business name from a domain, stripping TLD and common suffixes."""
    domain = _extract_domain(url)  # e.g. "johnbarnesgroup.au"
    if not domain:
        return None
    name_part = domain.split(".")[0]  # "johnbarnesgroup"
    # Strip common suffixes
    for suffix in BUSINESS_SUFFIXES:
        if name_part.endswith(suffix) and len(name_part) > len(suffix):
            name_part = name_part[:-len(suffix)]
            break
    return name_part  # "johnbarnes"
```

### 3. Optional: HTTP Redirect Verification

For higher confidence, follow redirects on the old domain to check if it resolves to the new one. This would be used as a secondary confirmation when the heuristic match is borderline.

- Only trigger when name matches but domains differ
- Use a short timeout (3-5s) to avoid blocking
- Cache results to avoid repeated lookups
- If the old domain redirects to the new one → definitive match, update supplier URL

**Trade-offs:** Adds network latency, old domain may be dead (no redirect), needs timeout handling. Consider making this opt-in or async.

## Files to Modify

| File | Change |
|------|--------|
| `includes/dashboard/models.py` | Add `domain_aliases` JSON column to Supplier |
| `alembic/versions/` | Migration for new column |
| `includes/dashboard/database.py` | `_extract_domain_stem()` helper, update `match_supplier()` domain-first and verification logic |
| `tests/test_database_matching.py` | Tests for multi-domain, stem matching, alias auto-append |

## Test Cases

1. **Exact domain match** (existing behaviour, should still work)
2. **Domain stem match**: `johnbarnesgroup.au` vs `johnbarnesqld.com.au` → same stem `johnbarnes` → accept
3. **Multi-domain lookup**: supplier has `domains: ["old.com.au", "new.com.au"]` → incoming `new.com.au` matches
4. **Auto-alias append**: when matched via stem, new domain added to `domain_aliases`
5. **Different business, similar name**: "ABC Parts" (`abcparts.com.au`) vs "ABC Fasteners" (`abcfasteners.com.au`) → different stems → reject
6. **Trigram-only match with domain mismatch** → still rejected (current strict behaviour)
7. **Containment match, completely different domains** → rejected (e.g. "Smith" at `smithtools.com` vs `acmeindustrial.com`)

## Priority

Medium — creates duplicate records which require manual cleanup, but the agent usually self-corrects on a second attempt. The multi-domain support is the more impactful part since it also prevents re-verification on every future match.
