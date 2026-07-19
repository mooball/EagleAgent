# Plan: Smart Item Adder — Dashboard Modal for RFQ Item Import

## TL;DR

Add a "Smart Add Items" button to the RFQ dashboard that opens a modal with a paste/drop area capable of capturing HTML tables, images, or plain text. Content is parsed server-side — HTML tables via BeautifulSoup (no LLM), images via a dedicated Gemini vision call, and plain text via CSV/TSV parsing. The user reviews an editable preview table before confirming. This replaces the error-prone "screenshot → chat → agent extraction" flow with a deterministic, user-validated pipeline.

---

## Problem

Today, users add items to RFQs by:
1. Taking a screenshot of a table (email, PDF, spreadsheet)
2. Uploading it to the Chainlit chat
3. The LLM agent extracts items via its generic vision prompt
4. The agent calls `manage_rfq(action='add_items', ...)` 

Problems with this flow:
- **Screenshot OCR is lossy** — vision model misreads characters (0↔O, 1↔l), skips rows, confuses columns
- **No human validation** — items are added directly; errors discovered later
- **Agent overhead** — full ReAct agent loop for what should be a simple extraction
- **HTML tables are wasted** — when a user copy-pastes an email table, only plain text survives the clipboard, losing the deterministic HTML structure that could be parsed with zero errors
- **No preview/edit** — if the LLM misreads "DHP486Z" as "DHP4B6Z", the user can't fix it inline

---

## Solution: Smart Item Adder Modal

A dedicated modal on the RFQ dashboard page that:
1. Accepts **HTML paste**, **image paste/drop**, or **plain text paste**
2. Routes content to the appropriate server-side parser
3. Shows an **editable preview table** of extracted items
4. User reviews, edits, and confirms → items added in one transaction

---

## Architecture

```
Dashboard (Alpine.js + HTMX)
│
├─ [Smart Add Items] button (new)
│     │
│     ▼
│  ┌─────────────────────────────────────────────┐
│  │  Modal (Alpine.js component)                │
│  │                                             │
│  │  ┌───────────────────────────────────────┐  │
│  │  │  Input area                            │  │
│  │  │  • contenteditable div (paste target)  │  │
│  │  │  • intercepts paste event → HTML+text  │  │
│  │  │  • click/drop zone for image upload    │  │
│  │  │  • plain text fallback                 │  │
│  │  └───────────────────────────────────────┘  │
│  │                    │                        │
│  │            [Extract Items]                   │
│  │                    │                        │
│  │         POST /api/rfq/{id}/extract-items     │
│  │                    │                        │
│  │  ┌───────────────────────────────────────┐  │
│  │  │  Editable Preview Table                │  │
│  │  │  ┌─────┬──────────────┬─────┬───────┐ │  │
│  │  │  │Line │ Description  │ Qty │ Brand │ │  │
│  │  │  ├─────┼──────────────┼─────┼───────┤ │  │
│  │  │  │  1  │ [Cordless..] │ [5] │ [Maki]│ │  │
│  │  │  │  2  │ [Impact Dr..]│ [3] │ [Maki]│ │  │
│  │  │  └─────┴──────────────┴─────┴───────┘ │  │
│  │  │  42 items extracted. All cells editable │  │
│  │  └───────────────────────────────────────┘  │
│  │                    │                        │
│  │         [Confirm & Add Items]                │
│  │                    │                        │
│  │        POST /api/rfq/{id}/items/bulk         │
│  └─────────────────────────────────────────────┘
│
▼
FastAPI Backend
│
├─ POST /api/rfq/{id}/extract-items   (new endpoint)
│     │
│     ├─ content_type: "html_table" ?
│     │    └─→ BeautifulSoup parser (deterministic, no LLM)
│     │
│     ├─ content_type: "image" ?
│     │    └─→ Gemini Vision + dedicated extraction prompt
│     │
│     └─ content_type: "plain_text" / "tsv" / "csv" ?
│          └─→ Python csv module parser (deterministic)
│
├─ POST /api/rfq/{id}/items/bulk      (new endpoint)
│     └─→ _add_items_sync() (existing function, one transaction)
```

---

## Phase 1: Backend — Content Detection & Extraction

### 1.0 New setting: `VISION_EXTRACTION_MODEL`

**File:** `config/settings.py`

Add a dedicated model setting for vision-based item extraction, consistent with the existing per-task model overrides (`QUOTE_PIPELINE_MODEL`, `BROWSER_AGENT_MODEL`, etc.):

```python
VISION_EXTRACTION_MODEL: str = os.getenv("VISION_EXTRACTION_MODEL", "gemini-2.5-flash")
```

This lets us swap the vision model independently (e.g., to a cheaper model for simple tables, or a more capable model for complex layouts) without touching extraction code.

### 1.1 New endpoint: `POST /api/rfq/{rfq_number}/extract-items`

**File:** `includes/dashboard/routes/rfqs.py`

**Request body:**
```json
{
  "html": "<table>...</table>",       // from paste event (text/html)
  "image_base64": "data:image/png;base64,iVBOR...",  // from paste/drop
  "plain_text": "Part\tDesc\tQty\n...",              // from paste event (text/plain)
  "filename": "items.csv"             // optional, helps detect format
}
```

At least one of `html`, `image_base64`, or `plain_text` must be present.

**Response:**
```json
{
  "content_type": "html_table",
  "column_mapping": {
    "Part #": "input_code",
    "Description": "input_description",
    "Qty": "quantity",
    "Brand": "brand"
  },
  "headers": ["Part #", "Description", "Qty", "Brand"],
  "fields": ["input_code", "input_description", "quantity", "brand"],
  "items": [
    {"input_code": "DHP486Z", "input_description": "Cordless Drill", "quantity": 5, "brand": "Makita"},
    {"input_code": "DTD154Z", "input_description": "Impact Driver", "quantity": 3, "brand": "Makita"}
  ],
  "item_count": 42,
  "warnings": [
    "Row 15: quantity 'N/A' treated as empty",
    "Row 31: missing description — skipped"
  ]
}
```

### 1.2 Content type detection

**File:** `includes/tools/rfq_item_import.py` (new file)

```python
def detect_content_type(html: str | None, image_base64: str | None, 
                        plain_text: str | None) -> str:
    """Detect whether content is an HTML table, image, or text."""
    # Priority: HTML table > image > structured text > plain text
    if html and ('<table' in html.lower() or '<tr>' in html.lower()):
        return "html_table"
    if image_base64:
        return "image"
    if plain_text:
        lines = plain_text.strip().split('\n')
        if len(lines) >= 2:
            # Detect delimiter by checking first line
            first = lines[0]
            if '\t' in first and first.count('\t') >= 1:
                return "tsv"
            if ',' in first and first.count(',') >= 1:
                return "csv"
        return "plain_text"
    return "unknown"
```

### 1.3 HTML table parser (no LLM)

**File:** `includes/tools/rfq_item_import.py`

Uses BeautifulSoup to parse `<table>` → extract `<tr>` → extract `<th>`/`<td>` → map columns.

Key behaviors:
- Auto-detects header row (has `<th>` cells)
- Column mapping via same `_COLUMN_PATTERNS` dictionary shared with `import_rfq_items`
- Merged cells (`colspan`, `rowspan`): duplicate the cell value into spanned positions
- Strips HTML formatting within cells (bold, links) — keeps just the text
- Handles nested tables by selecting the one with the most rows
- Returns detailed warnings: skipped rows, unparseable values

```python
def parse_html_table(html: str) -> dict:
    """Parse HTML table into structured items. Deterministic, zero LLM cost."""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find the best candidate table (most rows, has headers preferred)
    tables = soup.find_all('table')
    if not tables:
        return {"items": [], "warnings": ["No <table> found in HTML."]}
    
    # Score tables: prefer ones with <th> + many rows
    def score(t):
        has_th = 1 if t.find('th') else 0
        rows = len(t.find_all('tr'))
        return has_th * 1000 + rows
    
    best = max(tables, key=score)
    rows = best.find_all('tr')
    
    if len(rows) < 1:
        return {"items": [], "warnings": ["Table has no rows."]}
    
    # Header detection
    first_cells = rows[0].find_all(['th', 'td'])
    has_header = bool(rows[0].find('th'))
    
    headers = []
    if has_header:
        headers = [_clean_cell(c) for c in first_cells]
        data_rows = rows[1:]
    else:
        # Use first row as data but also as column hints
        headers = [f"col_{i}" for i in range(len(first_cells))]
        data_rows = rows  # treat all rows as data
    
    # Auto-detect column mapping
    column_mapping = _auto_detect_columns([h.lower() for h in headers])
    
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
            field = column_mapping.get(headers[col_idx].lower())
            if field:
                value = cell.get_text(strip=True)
                if value:
                    item[field] = value
        
        # Validate
        if not item.get('input_description'):
            warnings.append(f"Row {row_idx + (1 if has_header else 0)}: missing description — skipped")
            continue
        
        # Convert quantity to int if present
        if 'quantity' in item and item['quantity']:
            try:
                item['quantity'] = int(float(item['quantity']))
            except (ValueError, TypeError):
                warnings.append(f"Row {row_idx + (1 if has_header else 0)}: quantity '{item['quantity']}' is not a number")
                item['quantity'] = None
        
        items.append(item)
    
    # Build ordered fields list parallel to headers for frontend binding
    fields = [column_mapping.get(h.lower(), "") for h in headers]
    
    return {
        "content_type": "html_table",
        "column_mapping": {h: column_mapping.get(h.lower(), "") for h in headers},
        "headers": headers,
        "fields": fields,
        "items": items,
        "item_count": len(items),
        "warnings": warnings,
    }


def _clean_cell(cell) -> str:
    """Extract clean text from a <th> or <td>, stripping nested formatting."""
    return cell.get_text(strip=True)
```

### 1.4 Image extraction (Gemini Vision)

**File:** `includes/tools/rfq_item_import.py`

A single-purpose Gemini call with a focused extraction prompt — no ReAct agent overhead.

The model is configurable via `Config.VISION_EXTRACTION_MODEL` (see Phase 1.0 below), defaulting to `gemini-2.5-flash`. This allows swapping to a cheaper/faster model without code changes.

```python
MAX_IMAGE_SIZE_MB = 10  # Reject images larger than this

async def extract_items_from_image(image_base64: str, mime_type: str = "image/png") -> dict:
    """Use Gemini Vision to extract table data from a screenshot."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    from config.settings import Config
    
    # Guard against oversized images
    estimated_bytes = len(image_base64) * 3 / 4
    if estimated_bytes > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        return {"items": [], "warnings": [f"Image exceeds {MAX_IMAGE_SIZE_MB}MB limit. Please use a smaller image."]}
    
    model = ChatGoogleGenerativeAI(
        model=Config.VISION_EXTRACTION_MODEL,
        google_api_key=Config.GOOGLE_API_KEY,
        temperature=0,  # deterministic extraction
    )
    
    prompt = """Extract ALL rows from this table image as a JSON array.

Instructions:
1. First, identify the column headers visible in the image.
2. Extract EVERY visible row — do not skip, summarize, or truncate.
3. Map columns to these fields where possible:
   - "input_description": item description/name
   - "input_code": part number, SKU, product code
   - "brand": brand, manufacturer, make
   - "quantity": quantity, qty (as a number, not text)
   - "uom": unit of measure (ea, box, m, kg, etc.)
   - "notes": any additional notes or comments

4. If a cell is empty, omit the field (do not include "").
5. If you can't read a cell clearly, include your best guess and add a 
   "warnings" field noting the uncertainty.
6. Count the total visible rows and include the count.

Return ONLY valid JSON — no markdown, no explanation:
{
  "headers": ["Part #", "Description", "Qty", "Brand"],
  "items": [
    {"input_code": "DHP486Z", "input_description": "Cordless Drill", "quantity": 5, "brand": "Makita"},
    ...
  ],
  "item_count": 42,
  "warnings": ["Row 15: quantity cell is blurry — guessed 5"]
}"""
    
    # Build multimodal message
    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": f"data:{mime_type};base64,{image_base64}"}
    ])
    
    response = await model.ainvoke([message])
    
    # Parse JSON from response (strip markdown code fences if present)
    text = response.content
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    
    try:
        result = json.loads(text)
        result["content_type"] = "image"
        return result
    except json.JSONDecodeError:
        return {"items": [], "warnings": ["Failed to parse extraction result as JSON."]}
```

### 1.5 Plain text / CSV / TSV parser

**File:** `includes/tools/rfq_item_import.py`

Reuses the CSV parsing logic from `_import_csv_items_sync` (from the bulk operations plan), extended to handle tab-separated values:

```python
def parse_text_table(plain_text: str, content_type: str) -> dict:
    """Parse CSV, TSV, or plain text into structured items."""
    delimiter = '\t' if content_type == 'tsv' else ','
    
    reader = csv.reader(io.StringIO(plain_text), delimiter=delimiter)
    rows = list(reader)
    
    if not rows:
        return {"items": [], "warnings": ["No data found."]}
    
    # Auto-detect header
    headers = [h.strip().lower() for h in rows[0]]
    column_mapping = _auto_detect_columns(headers)
    
    # Check if first row looks like a header (contains known column names)
    has_header = any(field for field in column_mapping.values())
    
    if has_header:
        data_rows = rows[1:]
    else:
        # No mapping detected — treat first row as data, generate col_N headers
        headers = [f"col_{i}" for i in range(len(headers))]
        data_rows = rows
    
    # ... same extraction logic as HTML parser ...
    
    # Build ordered fields list parallel to headers for frontend binding
    fields = [column_mapping.get(h, "") for h in headers]
    
    return {
        "content_type": content_type,
        "headers": headers,
        "fields": fields,
        "column_mapping": {h: column_mapping.get(h, "") for h in headers},
        "items": items,
        "item_count": len(items),
        "warnings": warnings,
    }
```

### 1.6 Main extraction endpoint handler

**File:** `includes/dashboard/routes/rfqs.py`

```python
@router.post("/api/rfq/{rfq_number}/extract-items")
async def extract_rfq_items(
    rfq_number: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Extract items from pasted HTML, image, or text for preview."""
    from includes.tools.rfq_item_import import detect_content_type, parse_html_table, parse_text_table, extract_items_from_image
    
    body = await request.json()
    html = body.get("html")
    image_base64 = body.get("image_base64")
    plain_text = body.get("plain_text")
    
    if not any([html, image_base64, plain_text]):
        raise HTTPException(400, "At least one of html, image_base64, or plain_text is required.")
    
    # Image size guard (before base64 decoding)
    if image_base64:
        from includes.tools.rfq_item_import import MAX_IMAGE_SIZE_MB
        estimated_bytes = len(image_base64) * 3 / 4
        if estimated_bytes > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            raise HTTPException(413, f"Image exceeds {MAX_IMAGE_SIZE_MB}MB limit.")
    
    content_type = detect_content_type(html, image_base64, plain_text)
    
    if content_type == "html_table":
        result = parse_html_table(html)
    elif content_type == "image":
        # Strip data:image/...;base64, prefix if present
        if image_base64.startswith("data:"):
            mime_type = image_base64.split(";")[0].replace("data:", "")
            image_base64 = image_base64.split(",", 1)[1]
        else:
            mime_type = "image/png"
        result = await extract_items_from_image(image_base64, mime_type)
    elif content_type in ("csv", "tsv"):
        result = parse_text_table(plain_text, content_type)
    else:
        # Try all three plain text strategies
        result = parse_text_table(plain_text, "plain_text")
        if not result["items"]:
            result = parse_text_table(plain_text, "tsv")
        if not result["items"]:
            result = parse_text_table(plain_text, "csv")
    
    return result
```

---

## Phase 2: Backend — Bulk Add Endpoint

### 2.1 New endpoint: `POST /api/rfq/{rfq_number}/items/bulk`

**File:** `includes/dashboard/routes/rfqs.py`

A thin wrapper around the existing `_add_items_sync`:

```python
@router.post("/api/rfq/{rfq_number}/items/bulk")
async def bulk_add_rfq_items(
    rfq_number: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Add multiple items to an RFQ. Items come from the preview table."""
    from includes.tools.rfq_crud import _add_items_sync
    
    body = await request.json()
    items = body.get("items", [])
    
    if not items:
        raise HTTPException(400, "items list is required.")
    
    MAX_BATCH_SIZE = 200
    if len(items) > MAX_BATCH_SIZE:
        raise HTTPException(400, f"Too many items ({len(items)}). Maximum {MAX_BATCH_SIZE} per batch. Split into multiple imports.")
    
    result = await asyncio.to_thread(
        _add_items_sync, rfq_number, {"items": items}, user["email"]
    )
    
    if isinstance(result, str):
        raise HTTPException(400, result)
    
    return {"status": "ok", "item_count": len(items), "rfq": result}
```

---

## Phase 3: Frontend — Smart Item Adder Modal

### 3.1 Dashboard HTML — button placement

Add a button in the RFQ detail view (`includes/dashboard/routes/rfqs.py`, the `rfq_detail` template), likely near the existing item list or in the header area:

```html
<button @click="smartAddOpen = true"
        class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700">
    📋 Smart Add Items
</button>
```

### 3.2 Alpine.js modal component

**File:** Inline in the RFQ detail template, or as a partial returned by HTMX

```html
<template x-if="smartAddOpen">
<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
     @click.self="smartAddOpen = false">
  <div class="bg-white rounded-xl shadow-2xl w-full max-w-5xl max-h-[90vh] flex flex-col m-4">

    <!-- Header -->
    <div class="flex items-center justify-between p-4 border-b">
      <h2 class="text-lg font-semibold">Smart Add Items</h2>
      <button @click="smartAddOpen = false" class="text-gray-400 hover:text-gray-600 text-xl">&times;</button>
    </div>

    <!-- Step 1: Input Area -->
    <div class="p-4 border-b" x-show="!previewItems.length">
      <p class="text-sm text-gray-500 mb-3">
        Paste a table from an email, spreadsheet, or upload a screenshot.
        HTML tables are parsed automatically — no OCR needed.
      </p>
      <div class="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center
                  transition-colors"
           :class="dragOver ? 'border-indigo-400 bg-indigo-50' : ''"
           @dragover.prevent="dragOver = true"
           @dragleave="dragOver = false"
           @drop.prevent="handleDrop($event)">

        <!-- Paste target: contenteditable div captures HTML from clipboard.
             Note: contenteditable does not support the placeholder attribute natively.
             We use a CSS :empty:before pseudo-element for the placeholder text. -->
        <div contenteditable="true"
             @paste="handlePaste($event)"
             @input="handleInput($event)"
             class="paste-area min-h-[120px] max-h-[300px] overflow-y-auto text-left
                    border border-gray-200 rounded p-3 mb-3 focus:outline-none
                    focus:border-indigo-400"
             x-ref="pasteArea"></div>

        <!-- Placeholder style for empty contenteditable -->
        <style>
          .paste-area:empty:before {
            content: 'Paste table here (Ctrl+V / Cmd+V)...';
            color: #9ca3af;
            pointer-events: none;
          }
        </style>

        <div class="text-sm text-gray-400 mb-2">or</div>

        <!-- Image upload fallback -->
        <label class="inline-flex items-center gap-2 px-4 py-2 bg-gray-100
                      rounded-lg cursor-pointer hover:bg-gray-200 text-sm">
          📷 Upload Screenshot
          <input type="file" accept="image/*" class="hidden"
                 @change="handleFileUpload($event)">
        </label>
      </div>

      <div class="flex justify-end mt-4 gap-3">
        <button @click="smartAddOpen = false"
                class="px-4 py-2 text-gray-600 hover:text-gray-800">Cancel</button>
        <button @click="extractItems()"
                :disabled="extracting || !hasContent"
                class="px-4 py-2 bg-indigo-600 text-white rounded-lg
                       hover:bg-indigo-700 disabled:opacity-50">
          <span x-show="!extracting">Extract Items</span>
          <span x-show="extracting">Extracting...</span>
        </button>
      </div>
    </div>

    <!-- Step 2: Preview Table -->
    <div class="flex-1 overflow-auto p-4" x-show="previewItems.length">
      <div class="flex items-center justify-between mb-3">
        <span class="text-sm text-gray-600"
              x-text="previewItems.length + ' items extracted'"></span>
        <button @click="resetExtraction()"
                class="text-sm text-indigo-600 hover:text-indigo-800">← Try again</button>
      </div>

      <!-- Warnings -->
      <div x-show="warnings.length" class="mb-3">
        <template x-for="w in warnings">
          <div class="text-xs text-amber-700 bg-amber-50 border border-amber-200
                      rounded px-2 py-1 mb-1" x-text="w"></div>
        </template>
      </div>

      <!-- Editable table.
           Uses `fields` array (parallel to `headers`) for x-model binding,
           avoiding error-prone key lookups through columnMapping in the template. -->
      <div class="overflow-x-auto">
        <table class="w-full text-sm border-collapse">
          <thead>
            <tr class="bg-gray-50">
              <th class="border px-2 py-1 text-left text-xs text-gray-500">#</th>
              <template x-for="h in headers">
                <th class="border px-2 py-1 text-left text-xs text-gray-500"
                    x-text="h"></th>
              </template>
              <th class="border px-2 py-1 text-left text-xs text-gray-500 w-8"></th>
            </tr>
          </thead>
          <tbody>
            <template x-for="(item, idx) in previewItems">
              <tr>
                <td class="border px-2 py-1 text-gray-400 text-xs"
                    x-text="idx + 1"></td>
                <template x-for="(field, colIdx) in fields">
                  <td class="border p-0">
                    <input type="text"
                           class="w-full px-2 py-1 border-0 focus:ring-1 focus:ring-indigo-300
                                  focus:outline-none text-sm"
                           x-model="item[field]">
                  </td>
                </template>
                <!-- Delete row button -->
                <td class="border px-1 py-1 text-center">
                  <button @click="previewItems.splice(idx, 1)"
                          class="text-red-400 hover:text-red-600 text-xs"
                          title="Remove row">&times;</button>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer -->
    <div class="flex justify-between items-center p-4 border-t"
         x-show="previewItems.length">
      <button @click="resetExtraction()"
              class="px-4 py-2 text-gray-600 hover:text-gray-800">Cancel</button>
      <button @click="confirmAdd()"
              :disabled="adding"
              class="px-6 py-2 bg-green-600 text-white rounded-lg
                     hover:bg-green-700 disabled:opacity-50">
        <span x-show="!adding">Confirm & Add Items</span>
        <span x-show="adding">Adding...</span>
      </button>
    </div>

  </div>
</div>
</template>
```

### 3.3 Alpine.js component data & methods

```javascript
// Registered in base.html via alpine:init
Alpine.data('smartItemAdder', (rfqId) => ({
    // State
    smartAddOpen: false,
    dragOver: false,
    extracting: false,
    adding: false,
    pasteHtml: null,
    pasteText: null,
    imageBase64: null,
    previewItems: [],
    headers: [],
    fields: [],        // ordered field keys parallel to headers — used for x-model binding
    columnMapping: {},
    warnings: [],

    get hasContent() {
        return this.pasteHtml || this.pasteText || this.imageBase64;
    },

    // Paste handler — captures both HTML and plain text
    handlePaste(event) {
        const clipboard = event.clipboardData;
        if (!clipboard) return;

        // Capture HTML first (for table structure)
        const html = clipboard.getData('text/html');
        if (html && html.includes('<table')) {
            event.preventDefault();
            this.pasteHtml = html;
            this.pasteText = clipboard.getData('text/plain');
            // Show a visual indicator that HTML table was captured
            this.$refs.pasteArea.innerHTML = '✅ HTML table captured — click Extract Items';
        }
        // If no HTML table, let default paste behavior insert plain text
    },

    // Capture plain text from the contenteditable div
    handleInput(event) {
        this.pasteText = event.target.innerText || '';
    },

    // Drag and drop handler
    handleDrop(event) {
        this.dragOver = false;
        const files = event.dataTransfer.files;
        if (files.length > 0) {
            this.readImageFile(files[0]);
        }
    },

    // File upload handler
    handleFileUpload(event) {
        const file = event.target.files[0];
        if (file) this.readImageFile(file);
    },

    // Read image as base64
    readImageFile(file) {
        if (!file.type.startsWith('image/')) {
            alert('Please upload an image file (PNG, JPG, etc.)');
            return;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
            this.imageBase64 = e.target.result;
            this.$refs.pasteArea.innerHTML =
                `📷 Image captured: ${file.name} (${(file.size / 1024).toFixed(1)} KB) — click Extract Items`;
        };
        reader.readAsDataURL(file);
    },

    // Call extract endpoint
    async extractItems() {
        this.extracting = true;
        this.warnings = [];

        try {
            const body = {};
            if (this.pasteHtml) body.html = this.pasteHtml;
            if (this.imageBase64) body.image_base64 = this.imageBase64;
            if (this.pasteText) body.plain_text = this.pasteText;

            const resp = await fetch(`/api/rfq/${rfqId}/extract-items`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Extraction failed');
            }

            const data = await resp.json();
            this.previewItems = data.items || [];
            this.headers = data.headers || [];
            this.fields = data.fields || [];   // ordered field keys parallel to headers
            this.columnMapping = data.column_mapping || {};
            this.warnings = data.warnings || [];

            if (!this.previewItems.length) {
                alert('No items could be extracted. Try a different format or upload method.');
            }
        } catch (e) {
            alert('Extraction error: ' + e.message);
        } finally {
            this.extracting = false;
        }
    },

    // Confirm and add items to RFQ
    async confirmAdd() {
        this.adding = true;

        try {
            const resp = await fetch(`/api/rfq/${rfqId}/items/bulk`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items: this.previewItems }),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Failed to add items');
            }

            // Close modal and refresh the page (or trigger HTMX refresh)
            this.smartAddOpen = false;
            this.resetExtraction();

            // Trigger HTMX refresh of item list
            htmx.trigger('#rfq-items-container', 'refreshItems');
        } catch (e) {
            alert('Error adding items: ' + e.message);
        } finally {
            this.adding = false;
        }
    },

    resetExtraction() {
        this.previewItems = [];
        this.headers = [];
        this.fields = [];
        this.columnMapping = {};
        this.warnings = [];
        this.pasteHtml = null;
        this.pasteText = null;
        this.imageBase64 = null;
        if (this.$refs.pasteArea) {
            this.$refs.pasteArea.innerHTML = '';
        }
    },
}));
```

### 3.4 HTMX integration

After successful add, the RFQ item list should refresh. The template already has an `htmx.trigger('#rfq-items-container', 'refreshItems')` call. The RFQ detail page should have an HTMX endpoint that returns the item list as a partial:

```html
<!-- In the RFQ detail template -->
<div id="rfq-items-container"
     hx-get="/partial/rfq/{{ rfq.rfq_number }}/items"
     hx-trigger="load, refreshItems from:body"
     hx-swap="innerHTML">
  <!-- Item list rendered here -->
</div>
```

---

## Phase 4: Shared Column Detection

### 4.1 Extract shared `_COLUMN_PATTERNS` and `_auto_detect_columns()`

Move these from being duplicated across `rfq_crud.py` (CSV import from bulk plan) and `rfq_item_import.py` (this plan) into a shared location:

**File:** `includes/tools/rfq_item_import.py` (new shared module)

This module is imported by both `import_rfq_items` (in `rfq_crud.py`) and the `extract-items` endpoint.

```python
# Column auto-detection patterns — shared by CSV import and HTML table parser
_COLUMN_PATTERNS: dict[str, list[str]] = {
    "input_description": ["description", "item", "name", "product", "desc", "part name", "item description"],
    "input_code": ["part_number", "part #", "part no", "partno", "mpn", "sku", "code", "item code", "product code", "stock code"],
    "brand": ["brand", "make", "manufacturer", "mfr", "supplier"],
    "quantity": ["quantity", "qty", "qty req'd", "count", "qty required", "qty req"],
    "uom": ["uom", "unit", "unit of measure", "measure", "pack"],
    "notes": ["notes", "comment", "remarks", "additional info"],
}


def auto_detect_columns(headers: list[str]) -> dict[str, str]:
    """Map column header names to RFQ item fields.

    Returns dict of {header_name: field_name} for matched columns.
    Headers that don't match any pattern are omitted.
    
    Uses substring matching to handle real-world messy headers like
    "Item Description", "QTY REQ'D", "Part Number / SKU", etc.
    """
    mapping = {}
    for h in headers:
        h_lower = h.strip().lower()
        for field, patterns in _COLUMN_PATTERNS.items():
            # Substring match: pattern in header OR header in pattern
            if any(p in h_lower or h_lower in p for p in patterns):
                mapping[h.strip()] = field
                break
    return mapping
```

---

## Phase 5: Tests

### 5.1 HTML table parsing

- `test_parse_html_table_simple` — basic table with `<th>` header
- `test_parse_html_table_no_header` — table with only `<td>` cells
- `test_parse_html_table_merged_cells` — colspan/rowspan handling
- `test_parse_html_table_nested` — picks the largest table when nested
- `test_parse_html_table_missing_description` — skips rows with no description
- `test_parse_html_table_bad_quantity` — non-numeric quantity handled
- `test_parse_html_table_gmail_format` — real Gmail email table HTML

### 5.2 Image extraction

- `test_extract_from_image_clear_table` — clear table screenshot → items
- `test_extract_from_image_no_table` — non-table image → empty/warning

### 5.3 Text parsing

- `test_parse_tsv` — tab-separated values
- `test_parse_csv` — comma-separated values
- `test_parse_plain_text_fallback` — unstructured text

### 5.4 Content detection

- `test_detect_html_table` — detects `<table>` in HTML
- `test_detect_image` — detects base64 image
- `test_detect_tsv` — detects tab characters
- `test_detect_csv` — detects commas

### 5.5 Endpoint tests

- `test_extract_items_html` — POST with HTML → returns items
- `test_extract_items_image` — POST with image → returns items
- `test_extract_items_no_content` — POST with empty body → 400
- `test_bulk_add_items` — POST items → items added to RFQ in DB

---

## Relevant Files

| File | Change |
|---|---|
| `config/settings.py` | Add `VISION_EXTRACTION_MODEL` setting (default `gemini-2.5-flash`) |
| `includes/tools/rfq_item_import.py` | **New file** — `detect_content_type()`, `parse_html_table()`, `extract_items_from_image()`, `parse_text_table()`, `auto_detect_columns()`, `_COLUMN_PATTERNS`, `MAX_IMAGE_SIZE_MB` |
| `includes/dashboard/routes/rfqs.py` | Add `POST /api/rfq/{id}/extract-items` and `POST /api/rfq/{id}/items/bulk` endpoints |
| `includes/tools/rfq_crud.py` | Import `auto_detect_columns` and `_COLUMN_PATTERNS` from `rfq_item_import.py` (remove duplicates from Phase 2 CSV import) |
| `includes/dashboard/templates/` (or route template) | Add modal HTML, Alpine.js component, HTMX refresh trigger |
| `templates/base.html` | Register Alpine.js `smartItemAdder` component in `alpine:init` |
| `tests/tools/test_rfq_item_import.py` | **New file** — all extraction tests |

---

## UX Flow

```
1. User clicks [Smart Add Items] on RFQ detail page
       ↓
2. Modal opens with paste area
       ↓
3. User pastes table (Ctrl+V / Cmd+V)
   - If HTML table detected: green indicator "HTML table captured"
   - If plain text only: text appears in the area as-is
   OR user drops/clicks to upload a screenshot
       ↓
4. User clicks [Extract Items]
   - HTML → server parses with BeautifulSoup (< 100ms)
   - Image → Gemini extracts with dedicated prompt (2–3s)
   - Text → Python CSV parser (< 50ms)
       ↓
5. Preview table appears — all cells editable
   - Warnings shown (skipped rows, parse issues)
   - User can fix typos, adjust quantities, delete rows
       ↓
6. User clicks [Confirm & Add Items]
   - All items added in one DB transaction
   - Modal closes
   - Item list refreshes via HTMX
       ↓
7. User sees new items in the RFQ dashboard
```

---

## Verification

1. `uv run pytest tests/` — all tests pass
2. **Manual: Paste email table** — copy table from Gmail → paste → extract → all columns mapped → edit quantity → confirm → items appear
3. **Manual: Screenshot upload** — drag screenshot of a 50-row parts table → extract → preview shows all 50 rows → confirm
4. **Manual: Plain text paste** — copy CSV from Excel → paste → extract → items appear
5. **Manual: No content** — click Extract with empty input → error message
6. **Manual: Bad image** — upload a photo of a cat → Gemini returns empty items → warning shown

---

## Decisions

- **No LLM for HTML tables** — BeautifulSoup is deterministic, instant, and free. There's no reason to involve an LLM for structured `<table>` markup.
- **No LLM for CSV/TSV** — Python's `csv` module handles this perfectly. LLM only as a last resort for truly unstructured plain text.
- **LLM only for images** — Vision is the only way to read table data from a screenshot. But we use a dedicated, focused prompt instead of the generic agent prompt.
- **Contenteditable div for paste capture** — a `<div contenteditable>` intercepts the `paste` event and gives us access to `event.clipboardData.getData('text/html')`. A `<textarea>` cannot do this.
- **Separate extract + confirm** — extraction and adding are separate API calls. The user must explicitly confirm. This prevents bad data from entering the RFQ.
- **Editable preview** — every cell is an `<input>`. This is the safety net: if the parser or vision model makes a mistake, the user can fix it before confirming. Each row has a delete button (`×`) for removing unwanted rows.
- **Configurable vision model** — the image extraction model is set via `Config.VISION_EXTRACTION_MODEL` (env var override), not hardcoded. This follows the existing per-task model pattern (`QUOTE_PIPELINE_MODEL`, etc.).
- **`fields` array for clean binding** — the extract response includes a `fields` array ordered parallel to `headers` (e.g., `["input_code", "input_description", "quantity", "brand"]`). The preview table uses `x-model="item[field]"` instead of a fragile `columnMapping` key lookup.
- **Fuzzy column matching** — `auto_detect_columns()` uses substring matching (`pattern in header OR header in pattern`) instead of exact equality. This handles real-world messy headers like `"Item Description"`, `"QTY REQ'D"`, `"Part Number / SKU"`.
- **Image size limit** — images over 10MB are rejected before the Gemini API call. Prevents unnecessary cost and timeouts from oversized screenshots.
- **Batch size limit** — the bulk add endpoint caps at 200 items per request. Prevents accidental massive inserts from a vision model hallucinating hundreds of rows.
- **CSS placeholder for contenteditable** — since `contenteditable` divs don't support the `placeholder` HTML attribute, we use a `:empty:before` CSS pseudo-element.
- **Not replacing the chat path** — the chat-based `RFQ_ADD_ITEMS` flow continues to work. This is an additional, optimized path for dashboard users.
