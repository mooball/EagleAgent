# Skill Definition: RFQ Item Grouping

**Supplier Sourcing Group Assignment (v1.0)**

## 1. Objective

Given a list of line items on an RFQ (Request for Quote), assign each item to a **sourcing group** — a set of items that are very likely to come from the same supplier(s). The purpose is to avoid redundant supplier searches: instead of researching suppliers for every individual item, we research once per group.

**Critical principle: It is far safer to leave items ungrouped than to incorrectly group items that need different suppliers.** When in doubt, do NOT group.

## 2. Input

You will receive a JSON object with two keys:

### `items` — the line items to evaluate

A JSON array of **unevaluated** RFQ line items — items that have not yet been assigned to any group or explicitly marked ungrouped. Each item has:

| Field | Description |
|---|---|
| `line` | Line number (integer) |
| `input_description` | Free-text description of the item |
| `part_number` | Manufacturer part number (may be null) |
| `brand` | Manufacturer/brand name (may be null) |

Only items with status `confirmed` are passed for grouping. Quantity, unit of measure, and status are omitted as they are not relevant to sourcing group assignment.

### `existing_groups` — the current grouping state (may be null)

On the first run this will be `null`. On subsequent runs (when items have been added to the RFQ or re-confirmed after refinement), this will contain the previous grouping output — the same JSON structure described in section 3.

When `existing_groups` is provided:
- You must evaluate each item in `items` and either **add it to an existing group**, **create a new group**, or **mark it ungrouped**.
- Do NOT re-evaluate items already in existing groups. They have already been assessed. Only the items in the `items` array need decisions.
- You may create new groups if the unevaluated items don't fit any existing group but fit together with each other.
- You may add unevaluated items to different existing groups (they don't all have to go to the same place).
- Return the **complete, merged** grouping state — existing groups (with any newly added lines) plus any new groups, plus the updated ungrouped list.

## 3. Output

Return a JSON object with a single key `groups`, containing an array of group objects:

```json
{
  "groups": [
    {
      "id": "G1",
      "label": "Furukawa HB20G Hydraulic Breaker Parts",
      "reason": "All items are Furukawa OEM breaker components with Furukawa part numbering (002407-*, F22-*, HB20G-*). Same brand, same equipment type, same supply chain.",
      "lines": [1, 2, 3, 4, 5, 6, 7, 8]
    },
    {
      "id": "G2",
      "label": "Caterpillar Engine Bearings & Gaskets",
      "reason": "Confirmed Caterpillar parts with CAT part numbers. Engine rebuild components sharing the same OEM supply chain.",
      "lines": [9, 10, 11]
    }
  ],
  "ungrouped": [12, 15],
  "ungrouped_reason": "Line 12 is a generic bolt with no brand — could come from any supplier. Line 15 is a Toyota brake tube unrelated to any other items."
}
```

### Output rules

- The output must account for ALL lines — both the previously grouped/ungrouped lines from `existing_groups` and the newly evaluated lines from `items`. Every line must appear in exactly ONE group or in the `ungrouped` list. No duplicates. No omissions.
- Each group must have at least 2 lines. A single item cannot form a group — it goes in `ungrouped`.
- The `label` should be a concise human-readable description of what unites the group (brand + product category).
- The `reason` must explain WHY these items belong together, citing the specific evidence (shared brand, part number patterns, equipment context).
- When adding items to an existing group, append the new line numbers to that group's `lines` array and update the `reason` if needed to reflect the additions.

## 4. Grouping Rules

### 4.1. Primary signal: Brand

The strongest grouping signal is an **explicit, matching brand name**. Items confirmed as the same brand are strong candidates for the same group.

- "Furukawa" + "Furukawa" → group together
- "Caterpillar" + "Caterpillar" → group together
- "Caterpillar" + "Komatsu" → NEVER group together
- Null brand + "Caterpillar" → only group if there is strong contextual evidence (see 4.3)

### 4.2. Secondary signal: Part number patterns

Manufacturer part numbering schemes are highly informative:

- Shared prefixes (e.g. `002407-*`, `HB20G-*`) indicate parts from the same manufacturer and often the same equipment.
- Generic part numbers (e.g. plain `M12x40`) or missing part numbers are NOT a grouping signal.
- Cross-reference part numbers ("equivalent to CAT 211-0587") tell you the real OEM — use that brand for grouping.

### 4.3. Tertiary signal: Description context

Descriptions can reinforce or contradict a grouping decision:

- "Transfer Case Assembly for Isuzu FTS Truck" + "Front Drive Shaft Assembly for Isuzu FTS Truck" → same equipment context, group together.
- "Hydraulic Seal Kit" + "Engine Oil Filter" → different systems even if same brand — may still group by brand (same supplier), but note the sub-category difference in the label.
- "Bolt" or "O ring" with no brand or part number → ungrouped (too generic, could come from anywhere).

### 4.4. Items with no brand and no part number

All items passed for grouping have been confirmed, but some may still lack a brand or part number in the structured fields. If an item has **neither a brand nor a part number**, it is almost always ungrouped UNLESS:

- The description explicitly names a brand or equipment model that matches other items.
- Example: description "Kit Engine Gasket" with no brand, but ALL other items on the RFQ are Caterpillar engine parts → reasonable to infer this is also a Caterpillar part and include it in the group. State the inference explicitly in the reason.

Even in this case, err on the side of caution. If the inference is weak, leave it ungrouped.

### 4.5. Sub-grouping within the same brand

Sometimes a single brand should be split into sub-groups:

- **Do NOT sub-group** when all items are clearly from the same equipment or product family (e.g. all Furukawa breaker parts). One group is correct.
- **Do sub-group** when items from the same brand serve clearly different equipment or product lines that would be sourced from different divisions or dealers. For example:
  - "Komatsu engine filters" (lines 1-5) vs "Komatsu excavator bucket teeth" (lines 6-8) — these may come from different suppliers even though both are Komatsu. Create two groups.
- When unsure whether to sub-group, prefer a single group. The cost of one unnecessary supplier search is low; the cost of missing a shared supplier is high.

## 5. What NOT to group

**Never group items together just because they are both generic.** "Bolt M12" and "O-ring 25mm" are both generic, but they come from completely different suppliers. Generic items without a brand stay ungrouped.

**Never group across different brands** unless there is explicit evidence they share a supply chain (e.g. "Brand B is a known subsidiary of Brand A" or "this is an aftermarket part explicitly equivalent to Brand A"). When in doubt, don't.

**Never group based solely on the item being in the same RFQ.** The RFQ is a customer's wish list — it may contain items from completely unrelated supply chains.

## 6. Confidence and caution

- If you are >90% confident items share a supply chain → group them.
- If you are 60-90% confident → group them but note the uncertainty in the `reason` (e.g. "Likely Caterpillar based on description context, though brand is not confirmed").
- If you are <60% confident → leave ungrouped.

The goal is to save sourcing effort without making mistakes. A conservative grouping that misses some obvious clusters is acceptable. An aggressive grouping that mixes different supply chains is not.

## 7. Worked examples

### Example A: Single-brand parts list

Input: 67 items, all brand="FURUKAWA", descriptions like "Packing", "O ring", "Seal Kit", part numbers with prefixes `002407-*`, `F22-*`, `HB20G-*`, `160011-*`.

Correct output: **One group** containing all 67 lines. Label: "Furukawa Hydraulic Breaker Parts". These are all components for the same Furukawa breaker — identical brand, consistent part numbering, and all are breaker/hydraulic components.

### Example B: Engine rebuild with partial brand data

Input: 27 items. Lines 4, 7-9 have brand="Caterpillar" with CAT part numbers. Lines 1-3, 5-6, 10-27 have no brand but descriptions like "Kit Engine Gasket", "Cam Shaft Bearing", "Piston Pin", "Turbocharger", "Water Pump Kit".

Correct output: **One group** containing all 27 lines. Label: "Caterpillar Engine Rebuild Kit". Reason: The confirmed Caterpillar parts are engine internals (bearings, thrust plate, V-belt). The unconfirmed items are all engine rebuild components (pistons, gaskets, turbo, water pump) that contextually belong to the same engine overhaul. Given that 100% of identified parts are Caterpillar and all descriptions are engine-specific, it is highly likely these are all CAT parts.

### Example C: Mixed equipment RFQ

Input: 10 items. Lines 1-3 are "Komatsu 500-Hour Service Kit" components (filters, brand="Komatsu"). Lines 4-6 are "Corrosion Resistor", "Toyota Brake Tube", and "Sensor Angle A60H" (brands: none, Toyota, Volvo). Lines 7-10 are "Air Filter", "Fuel Filter" with brand="Komatsu".

Correct output:
- Group G1: Lines 1-3, 7-10. Label: "Komatsu Filters & Service Parts".
- Ungrouped: Lines 4, 5, 6. Reason: Line 4 has no brand. Line 5 is Toyota — different supply chain. Line 6 is Volvo — different supply chain. Each is an isolated item with no matching peers.

### Example D: All items are Isuzu truck drivetrain

Input: 3 items, all brand="Isuzu", descriptions reference the same truck model (FTS139-260) and serial number.

Correct output: **One group** containing all 3 lines. Label: "Isuzu FTS Truck Drivetrain Components". Same brand, same specific vehicle, same serial number.
