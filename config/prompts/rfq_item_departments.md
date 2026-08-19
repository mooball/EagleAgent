# Skill Definition: RFQ Item Department Classification

**Assign each RFQ line item to a NetSuite department (v1.0)**

## 1. Objective

Given RFQ line items that do not yet have a department, assign each item
to the single most appropriate department from the canonical list below.
Departments are pushed to NetSuite Opportunity line items and used for
internal reporting, so accuracy matters more than coverage.

**Critical principle: when you are not confident, omit the item.** It is
far better to leave an item without a department than to assign a wrong
one. Never use "Other Parts" as a default for uncertain items — it exists
for genuine miscellany only.

## 2. Departments

Use exactly the IDs from this table:

{{DEPARTMENT_TABLE}}

The Description column is the classification guidance. If an item fits
more than one department, prefer the more specific one (e.g. a turbocharger
is Engine Parts even though it sits in a truck; a tyre is always Tyres
regardless of vehicle type).

## 3. Input

A JSON object with an `items` array. Each item has:

- `line` — line number (integer)
- `input_description` — free-text description of the item
- `part_number` — manufacturer part number (may be null)
- `brand` — manufacturer/brand name (may be null)

## 4. Output

Return ONLY a JSON object with a single key `departments`, mapping line
numbers to department IDs:

```json
{
  "departments": {
    "1": "5",
    "3": "7"
  }
}
```

### Output rules

- Only include lines you are confident about. Omit unsure lines entirely.
- Keys must be the integer line numbers exactly as given in the input.
- Values must be one of the department IDs from the table in section 2.
  Never invent new IDs or use department names as values.
- No markdown fences, no commentary — just the JSON object.
