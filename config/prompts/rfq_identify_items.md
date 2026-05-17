# Skill Definition: RFQ Identify Items

**Web-based Item Identification (v1.0)**

Identify unidentified product(s) from the RFQ. For each item, search the web to verify the part number and find a positive product match.

## Part Number Validation
For each item, search the web to verify BOTH that:
1. The part number actually exists as a real product
2. The product that part number refers to matches the given description

For example, if the description says 'Hydraulic Return Filter' but the part number resolves to an oil filter or a completely different product, that is a mismatch.

## Review Flagging Rules
Flag an item for review (`status='review'`) if ANY of these are true:
- The exact part number cannot be found online
- The part number exists but refers to a different product than the description
- Similar/close part numbers exist that better match the description (possible typo)

In review cases, add a `notes` field explaining the issue (e.g. 'Part number not found. Closest matches: 201-60-71180, 201-01-71110' or 'Part number 600-211-2110 resolves to a fuel filter, not an oil filter as described').

## Status Assignment
For each item:
- **EXACT match AND description matches:** set part_number, brand, `status='confirmed'`
- **Part number wrong, missing, or mismatched to description:** set `status='review'` and `notes='...'` explaining the issue. Do NOT clear or remove the existing part_number or brand — keep them as-is so the user can see what was originally provided.
- **Cannot identify at all:** leave unchanged

Do NOT set `status='confirmed'` unless you are 100% certain the part number is correct AND matches the description.
