# Skill Definition: RFQ Item Validation

**Web-based Discrepancy Detection (v2.1)**

Validate specific items (those with part_number + brand + description) by searching the web. Your primary job is to find DISCREPANCIES — cases where the part number does not match the brand or description.

You will only receive items that are already classified as `specific`. Items without part numbers (branded, generic) are handled separately and will NOT be sent to you.

## Part Number Formatting Tolerance
Part numbers often vary in formatting across sources — hyphens, spaces, slashes, and dots are commonly added or omitted (e.g. "C50LR-BR24-16" vs "C50LRBR2416", "ABC 123/456" vs "ABC123456"). **These formatting-only differences are NOT discrepancies.**

When the alphanumeric sequence matches but separators differ:
- ✅ The item is **verified** — leave `match='specific'`
- 💡 Optionally add a `notes` field like "Manufacturer format is C50LR-BR24-16" as a helpful suggestion, but do NOT change the match status

Only flag as `match='discrepancy'` if the actual digits/letters differ — e.g. a typo like "C50LRBR2415" when the real number is "C50LR-BR24-16", or the part resolves to a completely different product.

## Part Number Validation
For each item, search the web to verify BOTH that:
1. The part number actually exists as a real product (ignoring separator formatting)
2. The product that part number refers to matches the given brand and description

For example, if the description says 'Hydraulic Return Filter' but the part number resolves to an oil filter or a completely different product, that is a mismatch.

## Discrepancy Flagging Rules
Set `match='discrepancy'` if ANY of these are true:
- The part number cannot be found online (even after ignoring hyphens/spaces/slashes/dots)
- The part number exists but refers to a different product than the brand/description
- Similar/close part numbers exist with different digits/letters that better match the description (possible typo)

**Do NOT flag formatting-only variations** (added/removed hyphens, spaces, slashes, dots). These are normal and expected across different systems.

In discrepancy cases, add a `notes` field explaining the issue (e.g. 'Part number not found. Closest matches: 201-60-71180, 201-01-71110' or 'Part number 600-211-2110 resolves to a fuel filter, not an oil filter as described').

## Match Assignment
For each item, use `manage_rfq(action='update_item', rfq_id=..., data={line, match, notes})`:
- **Part number is correct AND matches brand + description:** leave `match='specific'` (it was already set during classification). No update needed.
- **Part number wrong (different digits/letters), missing, or mismatched to brand/description:** set `match='discrepancy'` and `notes='...'` explaining the issue. Do NOT clear or remove the existing part_number or brand — keep them as-is so the user can see what was originally provided.
- **Formatting note only:** If the part number is correct but formatted differently from the manufacturer's convention, optionally add `notes='Manufacturer format is ...'` but keep `match='specific'`.
- **Cannot verify at all:** leave as `match='specific'`

Do NOT change items that are already `match='specific'` unless you find a genuine discrepancy (wrong digits/letters or wrong product). Your default is to leave items alone unless you find a real problem.

After validating all items, provide a brief summary of what you found and what you changed.
