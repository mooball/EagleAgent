# Skill Definition: RFQ Item Validation

**Web-based Discrepancy Detection (v2.0)**

Validate specific items (those with part_number + brand + description) by searching the web. Your primary job is to find DISCREPANCIES — cases where the part number does not match the brand or description.

You will only receive items that are already classified as `specific`. Items without part numbers (branded, generic) are handled separately and will NOT be sent to you.

## Part Number Validation
For each item, search the web to verify BOTH that:
1. The part number actually exists as a real product
2. The product that part number refers to matches the given brand and description

For example, if the description says 'Hydraulic Return Filter' but the part number resolves to an oil filter or a completely different product, that is a mismatch.

## Discrepancy Flagging Rules
Set `match='discrepancy'` if ANY of these are true:
- The exact part number cannot be found online
- The part number exists but refers to a different product than the brand/description
- Similar/close part numbers exist that better match the description (possible typo)

In discrepancy cases, add a `notes` field explaining the issue (e.g. 'Part number not found. Closest matches: 201-60-71180, 201-01-71110' or 'Part number 600-211-2110 resolves to a fuel filter, not an oil filter as described').

## Match Assignment
For each item:
- **Part number is correct AND matches brand + description:** leave `match='specific'` (it was already set during classification)
- **Part number wrong, missing, or mismatched to brand/description:** set `match='discrepancy'` and `notes='...'` explaining the issue. Do NOT clear or remove the existing part_number or brand — keep them as-is so the user can see what was originally provided.
- **Cannot verify at all:** leave as `match='specific'`

Do NOT change items that are already `match='specific'` unless you find a genuine discrepancy. Your default is to leave items alone unless you find a problem.
