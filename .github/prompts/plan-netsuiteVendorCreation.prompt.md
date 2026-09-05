# Plan: Create NetSuite Vendors (Suppliers) via REST

## Overview

Following the pattern of `includes/netsuite/records/item.py` (items/brands),
add the base functions to create **vendors** in NetSuite so the agent can
push suppliers from the local `suppliers` table into NetSuite.

The base function is deliberately conservative: **omit anything we are not
sure of**. Lookups (category, terms) are only sent when explicitly provided.
The eventual web UI will collect choices from the end user; the base function
must work headless with sensible defaults.

Based on live read-only probes against the NetSuite REST API (2026-09-03,
account 794882). Probe artifacts: `_probe_ns_vendor.py`, `_ns_vendor_schema.json`.

---

## Verified API Findings

### Write path

- `POST record/v1/vendor` is supported (swagger: `/vendor` → get/put/post/patch).
- `DELETE record/v1/vendor/{id}`: the route exists, but the current
  integration role lacks the **Lists → Suppliers** permission → **403**
  (verified live). Test vendors must be cleaned up in the NetSuite UI,
  or the integration role needs a higher permission level.
  (Contrast with `inventoryitem`, which has no DELETE operation at all.)
- Metadata schema exposes **185 properties**; none are flagged mandatory by
  the catalog. NetSuite REST docs require `companyName`; set `subsidiary`
  explicitly (single subsidiary = `{"id": "1"}`).
- Vendor schema file saved: `_ns_vendor_schema.json`.

### Field mapping (web form → REST)

| Web form field | REST property | Notes |
|---|---|---|
| Custom Form | `customForm` (ref) | Default id **97** (Master Supplier Form) |
| Company Name | `companyName` | Required |
| Type: Company / Individual | `isPerson` (boolean) | `false` = Company |
| Web address | `url` | |
| Category | `category` (ref) | Omit unless provided |
| Terms | `terms` (ref) | Omit unless provided |
| Phone | `phone` | |
| Email | `email` | Title "Supplier Email" |
| Primary Currency | `currency` (ref) | Default AUD (1) |
| Legal Name | `legalName` | Default = company name |
| Tax Code | `taxItem` (ref) | It is the **tax item**, not tax code |
| Currencies (multi) | `currencyList` sublist | items have `currency` ref; default AUD |
| Go Source Email Name | `custentity_go_souce_email_name` | Field ID literally contains the typo "souce" |
| Go Source Email Address | `custentity_go_souce_email_address` | same typo |

### Lookup lists

SuiteQL and REST **cannot** list `vendorCategory`, `term`, `currency` or
`taxitem` record types directly (not exposed). The lookup options were
derived from existing vendor data via
`SELECT DISTINCT <field>, BUILTIN.DF(<field>) FROM vendor`:

- **Terms**: 1 Net 14 Days, 2 30 Days, 4 Due on receipt, 7 Net 7 Days,
  8 Prepayment, 9 30 days EOM, 18 Payment after delivery, 19 End of Next
  Month (EOM), 20 45 Days from EOM, 21 Pay Immediately
- **Categories**: 1 Contractor, 2 Consultant, 3 Tax agency, 4 Supplies,
  5 Potential Supplier, 6 Tyre, 8 Parts, 9 Industrial, 10 AdBlue, 12 Diesel
- **Tax items**: 7 TS-AU, 9 FREE, 10 EXPS, 14 NCF-AU, 15 GST, 17 NA
- **Currencies**: 1 AUD (base), 2 USD, 3 CAD, 4 EUR, 5 NZD, 6 GBP, 8 JPY,
  9 SGD, 10 PHP, 11 ZAR

### Vendor custom forms (Custom Form dropdown)

From the NetSuite vendor form dropdown (`data-name="customform"`), 2026-09-03:

| Internal ID | Form name |
|---|---|
| 97 | 1 Master Supplier Form ⭐ (our default) |
| 114 | 1 Tyre Supplier Form |
| 55 | JCurve Payroll Vendor Form |
| 16 | JCurve Vendor Form (account default) |
| -20 | Standard Supplier Form |
| 30 | Wizard\|Vendor |

Notes:
- The leading "1 " in "1 Master Supplier Form" is a **display prefix**,
  not part of the ID.
- Forms are **role-restricted**: 97/114 initially fell back silently to 16
  when the integration role wasn't in the form's allowed roles. Fixed by
  adding the role (verified 2026-09-03).
- Custom forms are NOT queryable via SuiteQL or REST — this list must be
  maintained manually.

---

## Agreed rules

1. **Tax item**: local supplier (country AU) → GST (15); international →
   FREE (9). "International" = supplier country ≠ Australia.
2. **Omit lookups when not provided** — category and terms are only sent if
   the caller supplies them. No guessing.
3. **Type**: default Company (`isPerson=false`) when not provided.
4. **Currency**: default AUD (1) for `currency` and the `currencyList` when
   not provided.
5. **Local writeback**: yes — create/update the local `suppliers` row with
   `netsuite_id` on success (mirrors `item.py` product writeback).
6. **Category locally**: not yet — no `category` column on `suppliers` for now.

---

## Proposed base functions (`includes/netsuite/records/vendor.py`)

- `vendor_lookup_options()` → `{terms, categories, tax_items, currencies}`
  id↔name dictionaries (live `BUILTIN.DF` query, with the table above as
  documented reference).
- `resolve_tax_item(country)` → `15` (AU) else `9` (FREE).
- `resolve_currency(symbol)` → currency internal ID (default `1` AUD).
- `find_vendor_by_entity_id(name)` → SuiteQL exact `entityid` lookup.
- `create_vendor(company_name, *, is_person=False, url=None, category_id=None,
  terms_id=None, phone=None, email=None, currency_id=None, legal_name=None,
  tax_item_id=None, country=None, go_source_email=None, external_id=None,
  writeback_local=True) -> CreateResult`
  - payload: `companyName`, `subsidiary {"id": "1"}`, `isPerson`,
    `legalName` (default company name), `taxItem`, `currency` (default AUD),
    `currencyList` (default AUD), optional refs/strings, `externalId` for
    idempotency, custom go-source email fields.
- `ensure_vendor(...)` — find by entityId → exists: return id (optionally
  writeback); missing: create.

---

## Test plan

- `_test_ns_vendor_create.py` (throwaway): create `EGTEST VENDOR ...` vendors
  with the test rules (AU → GST, international → FREE), verify via
  `GET record/v1/vendor/{id}`, exercise find/ensure paths.
- DELETE via REST currently returns 403 (role permission) — clean test
  vendors up manually in the NetSuite UI. Local writeback rows are cleaned
  locally by the script.
- Verified 2026-09-03: AU vendor → taxItem 15 GST, INT vendor → taxItem 9
  FREE, currency 1 AUD, isPerson false — all correct. (Test vendors
  9946161 / 9946261 left for manual cleanup.)
- **Custom form**: "1 Master Supplier Form" = internal id **97** (the "1" is
  a display prefix). Form ids from the dropdown: 97 Master Supplier,
  114 Tyre Supplier, 55 JCurve Payroll, 16 JCurve Vendor, -20 Standard
  Supplier, 30 Wizard|Vendor. Forms 97/114 initially fell back to 16
  (role restriction) — fixed by adding the integration role to the form's
  allowed roles; verified 97 now sticks on create/patch.
