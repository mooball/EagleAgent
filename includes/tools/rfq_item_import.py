"""Shared RFQ item import utilities.

Used by both the dashboard Smart Item Adder modal (HTML/image/text paste)
and the bulk CSV import operations. Provides content detection, column
auto-mapping, and deterministic parsing for HTML tables, CSV/TSV text,
and Gemini Vision-based image extraction.
"""

import csv
import io
import json
import re
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

MAX_IMAGE_SIZE_MB = 10  # Reject images larger than this
MAX_BATCH_SIZE = 200      # Max items per bulk add request

# Standard RFQ column definition — the preview table ALWAYS shows these columns
# in this exact order. The LLM's job is to map source data into these fields.
_STANDARD_COLUMNS = [
    ("Description", "input_description"),
    ("Part Number", "input_code"),
    ("Brand", "brand"),
    ("Qty", "quantity"),
    ("UOM", "uom"),
]


# Column auto-detection patterns — shared by CSV import and HTML table parser.
# Maps internal RFQItem field names → list of common header aliases (lowercase).
_COLUMN_PATTERNS: dict[str, list[str]] = {
    "input_description": [
        "description", "item", "product", "desc",
        "part name", "item description", "line item",
    ],
    "input_code": [
        "part number", "part_number", "part #", "part no", "partno", "mpn", "sku",
        "code", "item code", "product code", "stock code",
        "supplier code", "manufacturer part no", "item #", "item number",
    ],
    "brand": [
        "brand", "make", "manufacturer", "mfr", "supplier",
    ],
    "quantity": [
        "quantity", "qty", "qty req'd", "count", "qty required",
        "qty req", "quantity required", "req qty",
    ],
    "uom": [
        "uom", "unit", "unit of measure", "measure", "pack",
        "unit size", "selling unit",
    ],
    "notes": [
        "notes", "comment", "remarks", "additional info", "extra",
    ],
}


def auto_detect_columns(headers: list[str]) -> dict[str, str]:
    """Map column header names to RFQ item fields.

    Uses STRICT exact matching (after normalisation) — no substring or fuzzy
    matching.  Only headers that match a known pattern exactly are mapped.

    Normalises separators (underscores, slashes → spaces) so
    "Part Number" matches "part_number" and "Qty Req'd" matches "qty req'd".

    Returns dict of {header_name: field_name} for matched columns.
    Headers that don't match any pattern are omitted.
    """
    def _norm(s: str) -> str:
        """Normalise a string: lowercase, replace non-alphanumeric separators
        with spaces, collapse whitespace."""
        return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', s.lower())).strip()

    mapping = {}
    for h in headers:
        h_norm = _norm(h)
        if not h_norm:
            continue
        for field, patterns in _COLUMN_PATTERNS.items():
            for p in patterns:
                p_norm = _norm(p)
                if p_norm == h_norm:
                    mapping[h.strip()] = field
                    break
            if h.strip() in mapping:
                break  # already matched, skip remaining fields
    return mapping


def _all_columns_mapped(headers: list[str], column_mapping: dict[str, str]) -> bool:
    """Return True only if EVERY header has a known field mapping.

    Used as a strict gate: if any column is unrecognised, we fall through
    to LLM analysis rather than guessing.
    """
    if not headers:
        return False
    return all(h.strip().lower() in column_mapping for h in headers if h.strip())


def _sanitize_field_name(header: str, index: int) -> str:
    """Return a safe field name for a column header.

    Strips non-alphanumeric chars and lowercases. Falls back to col_N
    if the header is empty or produces an empty result.
    """
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', header.strip()).strip('_').lower()
    return clean if clean else f"col_{index}"


def _normalize_to_standard_columns(result: dict) -> dict:
    """Enforce standard RFQ column headers and order on an extraction result.

    Replaces whatever headers/fields the LLM returned with the standard set:
    Description, Part Number, Brand, Qty, UOM. Items' data is preserved —
    only the display columns are standardized.
    """
    result["headers"] = [h for h, f in _STANDARD_COLUMNS]
    result["fields"] = [f for h, f in _STANDARD_COLUMNS]
    # Build column_mapping for standard headers
    result["column_mapping"] = {h: f for h, f in _STANDARD_COLUMNS}
    return result


def _extract_clean_table_html(html: str) -> str | None:
    """Extract the best candidate table and strip all styling/attributes.

    Returns clean structural HTML — only <table>, <tr>, <td>, <th> tags
    with text content. Preserves colspan/rowspan. Strips all CSS, inline
    styles, and wrapper elements.
    """
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')
    if not tables:
        return None
    def score(t):
        has_th = 1 if t.find('th') else 0
        return has_th * 1000 + len(t.find_all('tr'))
    best = max(tables, key=score)
    if len(best.find_all('tr')) < 1:
        return None
    for tag in best.find_all(True):
        keep = {}
        if tag.name in ('td', 'th'):
            if tag.get('colspan'): keep['colspan'] = tag['colspan']
            if tag.get('rowspan'): keep['rowspan'] = tag['rowspan']
        tag.attrs = keep
    return str(best)


# ============================================================================
# Content type detection
# ============================================================================

def detect_content_type(
    html: str | None,
    image_base64: str | None,
    plain_text: str | None,
) -> str:
    """Detect whether content is an HTML table, image, or text.

    Priority: HTML table > image > structured text > plain text.
    """
    if html and ('<table' in html.lower() or '<tr>' in html.lower()):
        return "html_table"
    if image_base64:
        return "image"
    if plain_text:
        lines = plain_text.strip().split('\n')
        if len(lines) >= 2:
            first = lines[0]
            if '\t' in first and first.count('\t') >= 1:
                return "tsv"
            if ',' in first and first.count(',') >= 1:
                return "csv"
        return "plain_text"
    return "unknown"


# ============================================================================
# HTML table parser (no LLM — deterministic)
# ============================================================================

def _clean_cell(cell) -> str:
    """Extract clean text from a <th> or <td>, stripping nested formatting."""
    return cell.get_text(strip=True)


def _parse_quantity(value) -> int | None:
    """Try to convert a value to an integer quantity. Returns None on failure.

    Handles strings ('5', '3.0', '~10', 'N/A'), ints, and floats.
    """
    if value is None:
        return None
    # Already a number
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    # String — strip noise and convert
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r'[~≈><]', '', value).strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def parse_html_table(html: str) -> dict:
    """Parse HTML table into structured items. Deterministic, zero LLM cost."""
    soup = BeautifulSoup(html, 'html.parser')

    # Find the best candidate table (most rows, has headers preferred)
    tables = soup.find_all('table')
    if not tables:
        return {"items": [], "headers": [], "fields": [],
                "column_mapping": {}, "item_count": 0,
                "warnings": ["No <table> found in HTML."]}

    # Score tables: prefer ones with <th> + many rows
    def score(t):
        has_th = 1 if t.find('th') else 0
        rows = len(t.find_all('tr'))
        return has_th * 1000 + rows

    best = max(tables, key=score)
    rows = best.find_all('tr')

    if len(rows) < 1:
        return {"items": [], "headers": [], "fields": [],
                "column_mapping": {}, "item_count": 0,
                "warnings": ["Table has no rows."]}

    # Header detection
    first_cells = rows[0].find_all(['th', 'td'])
    has_th = bool(rows[0].find('th'))
    first_row_texts = [_clean_cell(c) for c in first_cells]

    headers = []
    if has_th:
        # Explicit <th> — definitely a header row
        headers = first_row_texts
        data_rows = rows[1:]
        header_row_found = True
    elif len(first_row_texts) >= 2:
        # No <th> — check if first row looks like column headers
        probe_mapping = auto_detect_columns([h.lower() for h in first_row_texts])
        match_count = sum(1 for v in probe_mapping.values() if v)
        if match_count >= 2:
            # Multiple columns matched — treat first row as headers
            headers = first_row_texts
            data_rows = rows[1:]
            header_row_found = True
        else:
            headers = [f"col_{i}" for i in range(len(first_cells))]
            data_rows = rows
            header_row_found = False
    else:
        headers = [f"col_{i}" for i in range(len(first_cells))]
        data_rows = rows
        header_row_found = False

    # Auto-detect column mapping
    column_mapping = auto_detect_columns([h.lower() for h in headers])

    logger.info(
        f"HTML table: {len(data_rows)} data rows, headers={headers}, "
        f"mapping={column_mapping}"
    )

    # Strict gate: if any column is unrecognised, bail out so the
    # endpoint falls through to LLM analysis (which handles column mapping).
    if not _all_columns_mapped(headers, column_mapping):
        unmapped = [h for h in headers if h.strip().lower() not in column_mapping]
        return {"items": [], "headers": headers, "fields": [],
                "column_mapping": {}, "item_count": 0,
                "warnings": [f"Columns not recognised: {unmapped}. Falling through to LLM analysis."]}

    # Extract items
    items = []
    warnings = []
    for row_idx, row in enumerate(data_rows, start=1):
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue

        item = {}
        for col_idx, cell in enumerate(cells):
            if col_idx >= len(headers):
                break
            field = column_mapping.get(headers[col_idx].strip().lower())
            if field:
                value = cell.get_text(strip=True)
                if value:
                    item[field] = value

        # Validate: must have at least a description
        if not item.get('input_description'):
            warnings.append(
                f"Row {row_idx + (1 if header_row_found else 0)}: "
                f"missing description — skipped"
            )
            continue

        # Convert quantity to int if present
        if 'quantity' in item:
            qty = _parse_quantity(item['quantity'])
            if qty is None and item['quantity']:
                warnings.append(
                    f"Row {row_idx + (1 if header_row_found else 0)}: "
                    f"quantity '{item['quantity']}' is not a number — left empty"
                )
            item['quantity'] = qty

        items.append(item)

    # Build ordered fields list — unmatched columns get a sanitised fallback name
    # so every input gets a unique x-model binding (avoids all columns tied together)
    fields = [
        column_mapping.get(h.strip().lower()) or _sanitize_field_name(h, i)
        for i, h in enumerate(headers)
    ]

    return {
        "content_type": "image",
        "column_mapping": {h: column_mapping.get(h.strip().lower(), "") for h in headers},
        "headers": headers,
        "fields": fields,
        "items": items,
        "item_count": len(items),
        "warnings": warnings,
    }


# ============================================================================
# Plain text / CSV / TSV parser
# ============================================================================

def _validate_and_convert_row(
    item: dict, row_idx: int, warnings: list[str],
) -> dict:
    """Validate and clean a single parsed row. Returns None if invalid."""
    if not item.get('input_description'):
        warnings.append(f"Row {row_idx}: missing description — skipped")
        return None
    if 'quantity' in item:
        qty = _parse_quantity(item['quantity'])
        if qty is None and item['quantity']:
            warnings.append(
                f"Row {row_idx}: quantity '{item['quantity']}' "
                f"is not a number — left empty"
            )
        item['quantity'] = qty
    return item


def parse_text_table(plain_text: str, content_type: str) -> dict:
    """Parse CSV, TSV, or plain text into structured items."""
    delimiter = '\t' if content_type == 'tsv' else ','

    reader = csv.reader(io.StringIO(plain_text), delimiter=delimiter)
    rows = list(reader)

    if not rows:
        return {"items": [], "headers": [], "fields": [],
                "column_mapping": {}, "item_count": 0,
                "warnings": ["No data found."]}

    # Auto-detect header
    headers_raw = [h.strip() for h in rows[0]]
    headers_lower = [h.lower() for h in headers_raw]
    column_mapping = auto_detect_columns(headers_lower)

    # Strict gate: if ANY column is unrecognised, return 0 items so the
    # endpoint falls through to LLM analysis.
    if not _all_columns_mapped(headers_raw, column_mapping):
        unmapped = [h for h in headers_raw if h.strip().lower() not in column_mapping]
        return {"items": [], "headers": headers_raw, "fields": [],
                "column_mapping": {}, "item_count": 0,
                "warnings": [f"Columns not recognised: {unmapped}. Falling through to LLM analysis."]}

    # Check if first row looks like a header (contains known column names)
    has_header = any(column_mapping.values())

    if has_header:
        data_rows = rows[1:]
    else:
        # No mapping detected — treat first row as data, generate col_N headers
        headers_raw = [f"col_{i}" for i in range(len(headers_raw))]
        data_rows = rows
        column_mapping = {}

    # Extract items
    items = []
    warnings = []
    for row_idx, row in enumerate(data_rows, start=(2 if has_header else 1)):
        if not row or all(not c.strip() for c in row):
            continue

        item = {}
        for col_idx, cell in enumerate(row):
            if col_idx >= len(headers_raw):
                break
            field = column_mapping.get(headers_raw[col_idx].strip().lower())
            if field and cell.strip():
                item[field] = cell.strip()

        validated = _validate_and_convert_row(item, row_idx, warnings)
        if validated:
            items.append(validated)

    # Build ordered fields list — unmatched columns get a sanitised fallback name
    fields = [
        column_mapping.get(h.strip().lower()) or _sanitize_field_name(h, i)
        for i, h in enumerate(headers_raw)
    ]

    return {
        "content_type": content_type,
        "headers": headers_raw,
        "fields": fields,
        "column_mapping": {h: column_mapping.get(h.strip().lower(), "") for h in headers_raw},
        "items": items,
        "item_count": len(items),
        "warnings": warnings,
    }


# ============================================================================
# Image extraction (Gemini Vision)
# ============================================================================

_EXTRACTION_PROMPT = """Extract ALL rows from this table image into a strict JSON format.

You are parsing an industrial parts list, purchase order, or RFQ line items.

## Output Schema (STRICT — every item MUST use these exact field names)

Return a JSON object with this structure:

{
  "headers": ["Description", "Part Number", "Brand", "Qty", "UOM"],
  "items": [
    {
      "input_description": "Cordless Drill",
      "input_code": "DHP486Z",
      "brand": "Makita",
      "quantity": 5,
      "uom": "ea"
    }
  ],
  "item_count": 17,
  "warnings": []
}

## Field mapping (use these EXACT key names in every item)

| Column you see in the image | JSON key to use |
|---|---|
| Description / Item / Product / Name / Part Name | input_description |
| Part Number / Part # / MPN / SKU / Code / Item # / Stock Code | input_code |
| Brand / Make / Manufacturer / Mfr | brand |
| Quantity / Qty / Qty Req'd / Count | quantity (number, not text) |
| UOM / Unit / Pack / Measure | uom |
| Notes / Comments / Remarks | notes |

## Rules

1. Extract EVERY visible row — do not skip, summarize, or truncate.
2. Read cell text carefully. Do not confuse similar characters (0 vs O, 1 vs l).
3. If a cell is empty or unreadable, OMIT the field entirely — do NOT include "" or null.
4. quantity must be a NUMBER (5 not "five", 10 not "10 pcs"). No text in quantity.
5. **NO DATA LOSS**: When splitting a combined brand+code cell (e.g. "Makita DHP486Z 18V"),
   extract brand and input_code, then append any leftover text (like "18V", "2 pack",
   voltage, variant) to input_description. Every piece of information must be preserved.
6. The headers array is handled automatically — just return the actual column titles
   you see in the image.
7. If the image contains no table, return {"items": [], "warnings": ["No table found"]}.
8. Return ONLY valid JSON — no markdown code fences, no explanation text.
9. Every item MUST use the exact field names from the table above. Do not invent new keys."""


async def extract_items_from_image(
    image_base64: str, mime_type: str = "image/png",
) -> dict:
    """Use Gemini Vision to extract table data from a screenshot.

    Uses the configurable VISION_EXTRACTION_MODEL setting, falling back to
    DEFAULT_MODEL (same model the chat agent uses, which is proven to work).
    Temperature is forced to 0 for deterministic extraction.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.messages import HumanMessage
    from config.settings import Config

    # Guard against oversized images
    estimated_bytes = len(image_base64) * 3 / 4
    if estimated_bytes > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        return {
            "items": [], "headers": [], "fields": [], "item_count": 0,
            "column_mapping": {},
            "warnings": [
                f"Image exceeds {MAX_IMAGE_SIZE_MB}MB limit. "
                f"Please use a smaller image."
            ],
        }

    model_name = Config.VISION_EXTRACTION_MODEL or Config.DEFAULT_MODEL
    model = ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0,  # deterministic extraction
    )

    # Build multimodal message
    message = HumanMessage(content=[
        {"type": "text", "text": _EXTRACTION_PROMPT},
        {"type": "image_url", "image_url": f"data:{mime_type};base64,{image_base64}"},
    ])

    try:
        response = await model.ainvoke([message])
    except Exception as e:
        logger.error(f"Gemini API call failed: {e}")
        return {
            "items": [], "headers": [], "fields": [], "item_count": 0,
            "column_mapping": {},
            "warnings": [
                f"Vision model API call failed: {e}. "
                f"Check GOOGLE_API_KEY and VISION_EXTRACTION_MODEL settings."
            ],
            "content_type": "image",
        }

    # Parse JSON from response — content may be a string or a list of blocks
    raw_content = response.content
    if isinstance(raw_content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in raw_content
        )
    else:
        text = str(raw_content)

    logger.info(f"Gemini raw text (first 2000 chars): {text[:2000]}")

    # Strip markdown code fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"Gemini JSON parse failed: {e}")
        logger.warning(f"Raw Gemini text (first 500 chars): {text[:500]}")
        return {
            "items": [], "headers": [], "fields": [], "item_count": 0,
            "column_mapping": {},
            "warnings": ["Failed to parse extraction result as JSON."],
        }

    # Post-process: remap item keys from Gemini's header names to our field names,
    # validate quantities, build fields array
    items = result.get("items", [])
    warnings = result.get("warnings", [])
    headers = result.get("headers", [])

    logger.info(
        f"Gemini raw response: headers={headers}, "
        f"item_count={result.get('item_count')}, "
        f"first_item_keys={list(items[0].keys()) if items else 'N/A'}, "
        f"first_3_items={items[:3]}"
    )

    # Detect column mapping from returned headers
    column_mapping = auto_detect_columns([h.lower() for h in headers])
    logger.info(f"Column mapping: {column_mapping}")

    # Build a reverse lookup: Gemini's header name → our field name
    # Also handles case where Gemini used a synonym (e.g., "Part Number" → input_code)
    header_to_field = {}
    for h in headers:
        h_lower = h.strip().lower()
        field = column_mapping.get(h_lower)
        if field:
            header_to_field[h.strip()] = field
            # Also register lowercase/case variants
            header_to_field[h_lower] = field

    # Validate and remap each item
    cleaned_items = []
    for i, item in enumerate(items):
        # Remap keys from header names to field names
        remapped = {}
        for key, value in item.items():
            field = header_to_field.get(key.strip().lower())
            if field:
                remapped[field] = value
            else:
                # Unrecognised key — keep as-is (won't map to RFQ fields but
                # at least shows in preview under sanitised name)
                remapped[key] = value

        if 'quantity' in remapped:
            qty = _parse_quantity(remapped['quantity'])
            if qty is None and remapped['quantity']:
                warnings.append(
                    f"Row {i + 1}: quantity '{remapped['quantity']}' "
                    f"is not a number — left empty"
                )
            remapped['quantity'] = qty

        if remapped.get('input_description'):
            cleaned_items.append(remapped)
        elif any(remapped.values()):
            # Has data but no description — try to use whatever text is available
            warnings.append(f"Row {i + 1}: no description column found — skipped")

    fields = [
        column_mapping.get(h.strip().lower()) or _sanitize_field_name(h, i)
        for i, h in enumerate(headers)
    ]

    logger.info(
        f"Extraction result: {len(cleaned_items)} items, "
        f"fields={fields}, warnings={warnings}"
    )

    return _normalize_to_standard_columns({
        "content_type": "image",
        "column_mapping": {h: column_mapping.get(h.strip().lower(), "") for h in headers},
        "headers": headers,
        "fields": fields,
        "items": cleaned_items,
        "item_count": len(cleaned_items),
        "warnings": warnings,
        "_debug": {
            "raw_gemini_text": text[:2000],
            "raw_parsed_json": result,
            "column_mapping": column_mapping,
            "header_to_field": header_to_field,
        },
    })


# ============================================================================
# Smart text extraction (Gemini) — fallback for unstructured content
# ============================================================================

_TEXT_EXTRACTION_PROMPT = """Parse this pasted table text into structured line items for an RFQ.

The text comes from an email or spreadsheet that was copied and pasted.
It may contain asterisks (*bold*), irregular spacing, line numbers in the
first column, combined brand+part-number columns, and non-standard headers.

## Column format

The data typically appears in 4-5 columns:
1. Line number (optional — ignore)
2. Item description (the main product name)
3. Code column — may contain brand name, part number, or both combined
   (e.g. "Makita DHP486Z 18V", "Topcon brand", "Wiss M6M7AU 2 pack")
4. UOM (unit of measure: ea, set, pkt, m, etc.)
5. Quantity (number)

## How to extract

For each row:
- `input_description`: The item description text (column 2). Strip leading numbers if they're just line numbers.
- `input_code`: The part number/model code. If the code column contains both brand and code (e.g. "Makita DHP486Z"), split them: put the brand in `brand` and the code in `input_code`.
- `brand`: The brand/manufacturer name. Extract from the code column. If a cell says "Topcon brand", brand is "Topcon". If it says "Makita DHP486Z 18V", brand is "Makita".
- `quantity`: The quantity number.
- `uom`: The unit of measure.

## Pattern examples

| Raw text | brand | input_code | input_description |
|---|---|---|---|
| Makita DHP486Z 18V | Makita | DHP486Z | |
| Wiss M6M7AU 2 pack | Wiss | M6M7AU | |
| Topcon brand | Topcon | | |
| Topcon RL-H5A | Topcon | RL-H5A | |
| Paslode B20569V | Paslode | B20569V | |
| Makita starlock max B-66400-5 | Makita | B-66400-5 | |
| Macsim - Bunnings | Macsim | | |

## Rules

1. Ignore line numbers in the first column.
2. Text between asterisks is emphasis, not a separate field.
3. **NO DATA LOSS**: Split combined brand+code columns into separate `brand`
   and `input_code`, but any remaining text that doesn't fit cleanly into
   either field MUST be appended to `input_description`. For example:
   "Makita DHP486Z 18V" → brand="Makita", input_code="DHP486Z",
   append "18V" to input_description. "Topcon RL-H5A" → brand="Topcon",
   input_code="RL-H5A" (no leftover). "Wiss M6M7AU 2 pack" →
   brand="Wiss", input_code="M6M7AU", append "2 pack" to input_description.
4. If you can't determine the brand, leave it out.
5. `quantity` must be a number.
6. Output ONLY valid JSON — no markdown fences.

## Output format

{
  "headers": ["Description", "Code / Brand", "UOM", "Qty"],
  "items": [
    {"input_description": "Cordless drill - skin only", "input_code": "DHP486Z", "brand": "Makita", "quantity": 4, "uom": "ea"}
  ],
  "item_count": 30,
  "warnings": []
}"""


async def extract_items_from_text(plain_text: str) -> dict:
    """Use Gemini to parse plain text into structured items (unstructured email tables, etc)."""
    return await _llm_extract_items(
        _TEXT_EXTRACTION_PROMPT,
        f"Here is the text to parse:\n\n{plain_text[:8000]}",
    )


async def extract_items_from_html(html: str) -> dict:
    """Use Gemini to parse cleaned HTML table into structured items."""
    clean_html = _extract_clean_table_html(html)
    if not clean_html:
        return {"items": [], "headers": [], "fields": [], "item_count": 0,
                "column_mapping": {}, "warnings": ["No table found in HTML."]}
    return await _llm_extract_items(
        _TEXT_EXTRACTION_PROMPT,
        f"Here is an HTML table to parse:\n\n{clean_html[:8000]}",
    )


async def _llm_extract_items(prompt: str, content: str) -> dict:
    """Shared Gemini extraction for text or HTML content."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.messages import HumanMessage
    from config.settings import Config

    model_name = Config.VISION_EXTRACTION_MODEL or Config.DEFAULT_MODEL
    model = ChatGoogleGenerativeAI(model=model_name, temperature=0)

    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "text", "text": content},
    ])

    try:
        response = await model.ainvoke([message])
    except Exception as e:
        logger.error(f"Gemini LLM extraction failed: {e}")
        return {"items": [], "headers": [], "fields": [], "item_count": 0,
                "column_mapping": {}, "warnings": [f"Extraction failed: {e}"]}

    raw_content = response.content
    if isinstance(raw_content, list):
        text = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw_content)
    else:
        text = str(raw_content)

    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Gemini JSON parse failed, raw: {text[:500]}")
        return {"items": [], "headers": [], "fields": [], "item_count": 0,
                "column_mapping": {}, "warnings": ["Failed to parse extraction result."]}

    # Post-process: remap, validate, standardize
    items = result.get("items", [])
    warnings = result.get("warnings", [])
    headers = result.get("headers", [])
    column_mapping = auto_detect_columns([h.lower() for h in headers])

    header_to_field = {}
    for h in headers:
        h_lower = h.strip().lower()
        field = column_mapping.get(h_lower)
        if field:
            header_to_field[h.strip()] = field
            header_to_field[h_lower] = field

    cleaned_items = []
    for i, item in enumerate(items):
        remapped = {}
        for key, value in item.items():
            field = header_to_field.get(key.strip().lower())
            if field:
                remapped[field] = value
            else:
                remapped[key] = value
        if 'quantity' in remapped:
            qty = _parse_quantity(remapped['quantity'])
            if qty is None and remapped['quantity']:
                warnings.append(f"Row {i+1}: quantity '{remapped['quantity']}' not a number")
            remapped['quantity'] = qty
        if remapped.get('input_description'):
            cleaned_items.append(remapped)
        elif any(remapped.values()):
            warnings.append(f"Row {i+1}: no description — skipped")

    fields = [
        column_mapping.get(h.strip().lower()) or _sanitize_field_name(h, i)
        for i, h in enumerate(headers)
    ]

    return _normalize_to_standard_columns({
        "content_type": "llm",
        "headers": headers,
        "fields": fields,
        "column_mapping": {h: column_mapping.get(h.strip().lower(), "") for h in headers},
        "items": cleaned_items,
        "item_count": len(cleaned_items),
        "warnings": warnings,
    })
