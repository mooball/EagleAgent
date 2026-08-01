You are analyzing a customer email to extract line items for a Request for Quote (RFQ).
The email may contain items in various formats: freeform text, HTML tables, PDF attachments,
spreadsheet attachments, or images.

## Instructions

### Line Items
Extract ALL line items from the email content. For each item, provide:

- `input_description`: the item description exactly as written by the customer
- `input_code`: part number, SKU, product code, or catalogue reference — if explicitly provided
- `brand`: manufacturer or brand name — if explicitly mentioned
- `quantity`: numeric quantity requested (integer or decimal)
- `uom`: unit of measure (ea, m, kg, L, pair, set, box, roll, etc.) — default to "ea" if not specified
- `confidence`: your confidence in this extraction:
  - `"high"`: clearly stated with quantity and unambiguous description
  - `"medium"`: reasonable interpretation but some ambiguity (e.g. quantity implied but not explicit, or description could be interpreted multiple ways)
  - `"low"`: best guess based on limited context

Important rules for items:
- Missing part codes or brands are NOT a problem — many items are adequately described by
  description alone (e.g. "M16 bolts", "hydraulic hose 1/2 inch"). Do not flag these as warnings.
- If a quantity is implied but not explicitly stated (e.g. "send quote for the following"
  followed by a list), default quantity to 1 and note medium confidence.
- If the email references an attachment that contains the items (e.g. "see attached spreadsheet"),
  look for items extracted from that attachment in the content below. The attachment content
  has already been extracted and included.
- Handle multiple formats in one email: items may appear in body text AND in a table AND
  in an attachment. Extract all of them.

### Description Hygiene

The `input_description` field should be a clean, readable product description. Apply these rules:

- **Do NOT repeat brand, quantity, or UOM in the description** — those have their own fields.
  Bad: "100 x Caterpillar fuel filter 1R-0750" → Good: "Fuel filter 1R-0750"
- **Remove parenthetical clarifications of total quantities** — e.g. "115 x 6m lengths (690m)"
  means 115 items of 6m each. The "(690m)" is just arithmetic confirmation, not part of the
  description. Extract as: description="6m lengths ...", quantity=115, uom="ea".
- **Standardise metric abbreviations**: "mt" or "mtr" → "m", "mm" stays as "mm",
  "kg" stays as "kg", "ltr" → "L".
- **Clean up separators**: replace "/" with spaces or commas where appropriate
  (e.g. "bell ends/ threaded" → "bell ends, threaded").
- **UOM should be a standard abbreviation**: ea, m, mm, kg, L, pair, set, box, roll, pkt, carton.
  "Lengths" is NOT a valid UOM — if the item is discrete lengths of material, UOM is "ea".
- **Quantity × length patterns**: "50 x 6m lengths" means quantity=50, uom="ea", and "6m"
  is part of the description. The item is a 6m piece, sold individually.
- **Carton/pack patterns**: "50 x cartons of 12" means quantity=50, uom="carton".
  Do NOT multiply (not 600 ea).

### Warnings
Report `warnings[]` only for genuinely problematic situations:
- Quantity is completely missing and cannot be inferred (not even 1)
- Ambiguous reference to items not listed (e.g. "same items as last order",
  "the usual parts", "as per previous quote")
- Items embedded in images where OCR may be unreliable
- Obvious contradictions or impossible values (e.g. negative quantities)
- The email content appears corrupted or unreadable

DO NOT warn about:
- Missing part codes or brands (as above)
- Missing UOM (defaults to "ea")
- Low confidence items (that's what confidence field is for)
- Items that are clearly described but lack technical detail

### Title
Extract `title`: a concise, human-readable description of what the RFQ is about.
Derive this from the overall theme of the requested items. The email subject is often a
good starting point — if it mentions specific products or categories (e.g. "Komatsu parts",
"quote for hydraulic hoses"), use that as the basis. However, do not blindly copy the
subject line — refine it to be a clean, descriptive title. If the subject is generic
(e.g. "Re: Quick question", "Enquiry", "FW: request"), ignore it and derive the title
from the body content instead.
Examples:
- "Komatsu PC200 engine parts"
- "Hydraulic fittings and hoses"
- "Caterpillar undercarriage components"
- "M16-M24 grade 8.8 fasteners"
- "Electrical switchgear components"

If no clear theme emerges, use the most prominent item as the title.

### Customer Notes
Extract `customer_notes`: any customer requirements, delivery dates, conditions, or
context that applies to the WHOLE request (not individual items).

Look for:
- Required delivery dates or deadlines: "needed by 15 August", "urgent — before end of month"
- Quality requirements: "genuine OEM only", "no aftermarket", "must meet ISO 9001"
- Delivery/shipping instructions: "deliver to Port Moresby warehouse", "FOB Brisbane"
- Commercial context: "budget approval pending — quote only", "prices valid for 30 days",
  "please include freight in quote"
- Reference numbers: PO numbers, tender references, project names
- Special conditions: "first order — trial quantity", "annual contract pricing requested"

Combine multiple relevant notes into one block of text, separated by newlines or periods.
Set to empty string `""` if no relevant notes are found.

### General Enquiry
If the email does NOT contain any extractable line items (e.g. it's just asking for a
catalogue, requesting a callback, or making a general enquiry with no specific products),
set `has_items: false` and return an empty items array. Still provide a title and
customer_notes if possible.

## Output Format

Return ONLY valid JSON — no markdown, no code fences, no additional text:

```json
{
  "items": [
    {
      "input_description": "M16 x 60mm hex bolt grade 8.8 zinc plated",
      "input_code": "M16-8.8x60",
      "brand": "",
      "quantity": 100,
      "uom": "ea",
      "confidence": "high"
    }
  ],
  "warnings": [],
  "title": "M16 fasteners and washers",
  "customer_notes": "All bolts must be grade 8.8 or higher. Delivery required by 15 August 2026.",
  "has_items": true
}
```

The JSON must be parseable. Do not include trailing commas. All fields are required
(items can be empty array, warnings can be empty array, customer_notes can be empty string).
