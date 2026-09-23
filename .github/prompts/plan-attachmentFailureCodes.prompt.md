# Plan: Typed attachment-failure recording (RFQ-creation input completeness)

> Status: **IMPLEMENTED** (2026-09-23) — reviewed and built in one pass.
> Related: [plan-rfqCreationPipeline.prompt.md](plan-rfqCreationPipeline.prompt.md),
> [plan-pipelineReliability.prompt.md](plan-pipelineReliability.prompt.md),
> [plan-rfqAgentWorkingLock.prompt.md](plan-rfqAgentWorkingLock.prompt.md)
> Scope: record **what the pipeline could not read** from an incoming email, in a
> machine-readable form, so it can surface as a warning today and drive a
> confidence score later. Does **not** attempt to recover from model failures.
> Predecessor to the confidence-scoring work (§10).
> As-built deviations from this document: see §11.

---

## 0. Approved decisions (2026-09-23)

Answers given during scoping. These are settled; §8 only lists what is still open.

| # | Question | Decision |
|---|---|---|
| 1 | Purpose of the signal | Today: warn the user to double-check the email. Later: gate automation on a threshold. Ultimately: learn/tune extraction quality. So it must be **threshold-comparable and analysable**, not just human prose. |
| 2 | Granularity | **Both** — an RFQ-level summary, plus a note on specific low-confidence lines. (§10; this phase only lays the foundation.) |
| 3 | Failure handling | A visible warning is **enough for now** — recovering from model failures is a separate future task. But failures must be **recorded correctly**: distinct codes, not free text. |
| 4 | Shape | Copy the supplier-dedup pattern: a **score** (for gating) **and** reasons. |
| 5 | `EMPTY` vs `MODEL_ERROR` | **Separate.** "The model couldn't read it" and "there was nothing in it" mean different things for gating. |
| 6 | Surfacing in this phase | **Comms panel only** (the existing RFQ-Creation-Pipeline modal already renders `warnings`). RFQ-page surfacing comes with the confidence indicator, so the display is built once. |

Also agreed: **do this before confidence scoring.** A score computed over an
input that silently lost an attachment would be confidently wrong.

---

## 1. Background

### The observed failure

From a real local run of the create-RFQ pipeline (email #49663, 10 attachments):

```
Gemini PDF extraction failed for estimate QBRI1207.pdf: 500 INTERNAL.
{'error': {'code': 500, 'message': 'Internal error encountered.', 'status': 'INTERNAL'}}
```

The RFQ was created with **1 item instead of 2**. That is correct given the
input — the extractor never saw the PDF — but nothing anywhere says so. The
`warnings` list was empty, the RFQ looked normal, and the shortfall is
indistinguishable from "the email really only had one line".

### Why it is invisible today

`_extract_email_content_sync()` (`includes/tools/supplier_quote_pipeline.py:378`)
builds one Markdown bundle for the extractor. Per attachment:

```python
extracted = extract_pdf_content(raw_bytes, filename, pipeline="QUOTE")
parts.append(f"{header}\n\n{extracted}")
```

The extractors (`includes/email_pipeline.py`) **swallow failures and return a
marker string** — `extract_pdf_content:284` returns
`*[PDF extraction failed: {e}]*`, `extract_image_content:328` returns
`*[Image extraction failed: {e}]*`, and spreadsheets (`:359`) return
`*[Spreadsheet extraction failed: {e}]*`. Some are `logger.error`'d; none are
reported upward.

So the failure marker is **content**: it is appended to the bundle and fed to
the extraction LLM, and it is the only trace that anything went wrong.

### The root problem: three inconsistent, stringly-typed shapes

| Level | Today | Consumed by |
|---|---|---|
| Attachment | `*[PDF extraction failed: {e}]*` embedded **in the content** | the LLM prompt |
| Bundle | `"Error: Email not found"` | `caller.startswith("Error:")` — prefix sniffing on prose |
| Bundle | `"Error: No content found in email (no body or attachments)"` | same prefix sniff |
| Attachment | `*[Failed to fetch attachment]*` | the LLM prompt |
| Attachment | `*[Unsupported attachment type: {mime}]` | the LLM prompt |
| Image | skipped as signature | nothing (correct — not a failure) |

Two defects follow. **Content and metadata are conflated**, so a caller cannot
tell a failed attachment from one whose text legitimately contains "PDF
extraction failed". And there is **no code to branch on**, so "the pipeline read
9 of 10 attachments" is not representable — which is exactly what the confidence
work needs (§10).

This is the HTTP-status problem: the value of a 4xx is not the number, it is
that the *class* of failure lives in a small closed set. The equivalent here is a
small enum plus a detail string.

---

## 2. Goals

1. **Classify failures** into a closed set of codes at two levels: bundle and
   attachment.
2. **Separate metadata from content** — the bundle keeps a neutral placeholder;
   the code and detail live in a structured record.
3. **Record it** on `email_tracking.rfq_creation_result` under `input`, in a
   shape a later score can consume.
4. **Surface it now** as a human warning line, which the existing comms-panel
   modal already renders — no new UI.
5. **No behaviour change** to what gets extracted, and **no schema change**.

### Non-goals

- Retrying, repairing, or working around model failures (separate future task).
- OCR fallback / alternate models / PDF page-splitting.
- Any user-visible change on the RFQ page (§10 does that).
- Changing what the extractors *extract* — only what they *report*.

---

## 3. Design

### 3.1 Failure taxonomy

Two enums in `includes/email_pipeline.py` (shared infrastructure — the QUOTE
pipeline uses the same path).

```python
class AttachmentFailure(str, Enum):
    FETCH_FAILED = "fetch_failed"   # bytes couldn't be downloaded from Gmail
    UNSUPPORTED  = "unsupported"    # mime type we don't handle at all
    MODEL_ERROR  = "model_error"    # LLM raised after retries (5xx, timeout, ...)
    EMPTY        = "empty"          # LLM returned nothing / document was blank
    PARSE_ERROR  = "parse_error"    # local parsing failed (xlsx / csv)

class BundleFailure(str, Enum):
    EMAIL_NOT_FOUND = "email_not_found"
    NO_CONTENT      = "no_content"  # no body AND no readable attachments
```

**`MODEL_ERROR` vs `EMPTY`** (decision 5): the first is a transient
infrastructure failure and a candidate for retry; the second is an outcome. For
gating they differ — an `EMPTY` scanned PDF may be legitimately blank, whereas a
`MODEL_ERROR` means we don't know what was in it.

**`UNSUPPORTED` is a gap, not an error** — deterministic, and it will not change
on retry. The phase-2 score should be able to weight `unsupported` differently
from `model_error`; that is why they are distinct codes rather than a boolean.

### 3.2 Structured extraction result

Change the three extractors to return text **and** an optional failure, instead
of a marker string:

```python
@dataclass
class AttachmentExtraction:
    text: str                              # what goes into the bundle
    failure: AttachmentFailure | None = None
    detail: str = ""                       # e.g. "500 INTERNAL"
```

- Success → `AttachmentExtraction(text=<extracted>)`
- `MODEL_ERROR` → `AttachmentExtraction("*[Attachment could not be read]*",
  MODEL_ERROR, detail=str(e))`
- `EMPTY` → `AttachmentExtraction("*[No content extracted]*", EMPTY)`
- `PARSE_ERROR` → `AttachmentExtraction("*[Spreadsheet could not be parsed]*",
  PARSE_ERROR, detail=str(e))`

`detail` is for humans and logs, never for branching.

**Consequence to accept:** this is a signature change. Call sites are small —
one production caller plus 8 spreadsheet tests in `tests/test_email_pipeline.py`
(`test_csv_basic`, `test_csv_truncation`, `test_xlsx_basic`, `test_xlsx_empty_sheet`,
`test_xlsx_none_cells`, `test_xlsx_multi_sheet`, `test_xlsx_truncation`,
`test_corrupt_file`) which become `result.text` / `result.failure`. Listed in §7.

### 3.3 `ContentBundle` and a backward-compatible wrapper

```python
@dataclass
class ContentBundle:
    text: str
    attachment_total: int = 0
    attachment_read: int = 0
    skipped_as_signature: int = 0
    failures: list[AttachmentExtraction] = field(default_factory=list)
    bundle_failure: BundleFailure | None = None

def extract_email_content(email_tracking_id: int) -> ContentBundle: ...
```

`_extract_email_content_sync()` becomes a thin wrapper returning `.text`, so the
QUOTE pipeline, `scripts/test_supplier_pipeline.py` and existing tests are
untouched. It keeps its current self-heal behaviour
(`_backfill_email_content_from_gmail`) and its `quote_attachments` deprecation
note.

`attachment_read` counts attachments whose content actually reached the bundle.
Triaged-and-skipped signatures are counted separately and are **not** failures.

### 3.4 Clean placeholder in the bundle

Replace the raw error text in the bundle with a neutral
`*[Attachment could not be read]*`. Two reasons: upstream error detail stops
leaking into the prompt, and the marker no longer doubles as the failure signal.
The model still gets told the attachment is unavailable, which is better than
silence.

### 3.5 Recording on the result

`_extract_rfq_items_sync()` (`includes/tools/rfq_creation_pipeline.py:513`)
currently returns `(items, llm_result)`; stage 4 assembles the stored result at
`:414-445`. Rather than change that signature (callers and tests depend on it),
carry the report on `llm_result` under a private key, matching the existing
`_raw_response` convention:

```python
llm_result["_input_report"] = {...}      # set in _extract_rfq_items_sync
result["input"] = llm_result.get("_input_report")   # stage 4
```

Stored shape:

```json
"input": {
  "attachment_total": 10,
  "attachment_read": 9,
  "skipped_as_signature": 0,
  "failures": [
    {"filename": "estimate QBRI1207.pdf", "code": "model_error",
     "detail": "500 INTERNAL"}
  ],
  "bundle_failure": null
}
```

On the error/partial paths (`extraction failed`, `_save_error`), `input` is
absent or minimal — those already carry `status: error` and an `error` string.

### 3.6 Surfacing (comms panel only — decision 6)

A human line is appended to the existing `warnings` list, next to the existing
`warnings.append(...)` calls:

> `Could not read estimate QBRI1207.pdf (model_error: 500 INTERNAL) — items in that document may be missing`

The RFQ-Creation-Pipeline modal in `templates/partials/rfq_detail.html:966` and
`templates/partials/admin_emails.html` already renders `rd.warnings`, so this
appears with no template work.

Optional and cheap: a small structured block in the same modal reading
`rd.input` (`9 of 10 attachments read`) — a few lines, same component. Included
unless the reviewer would rather keep this phase to the warning line alone.

---

## 4. What gets recorded

| Situation | Code | Counted as read? | Warning? |
|---|---|---|---|
| Attachment extracted fine | — | ✅ yes | no |
| Image triaged as signature | — | ❌ (skipped, counted separately) | **no** |
| Bytes couldn't be downloaded | `fetch_failed` | ❌ | yes |
| Mime type we don't handle | `unsupported` | ❌ | yes |
| LLM raised after retries (5xx/timeout) | `model_error` | ❌ | yes |
| LLM returned empty / blank document | `empty` | ❌ | yes |
| xlsx/csv local parse failed | `parse_error` | ❌ | yes |
| Email row missing | `email_not_found` (bundle) | — | yes |
| No body and no readable attachments | `no_content` (bundle) | — | yes |

---

## 5. Edge cases

- **Multiple failures** — one entry per attachment; the warning line is emitted
  once with a count plus the first few filenames, not one line per file.
- **Signature triage must not be a failure.** A 10-attachment email with 3
  logos is fully read, not "7 of 10".
- **Forwarded/threaded bodies** — unaffected; this touches attachment dispatch
  only.
- **`detail` contains a newline / very long Google error** — truncate (e.g. 200
  chars) before storing, so the JSONB stays readable.
- **Non-UTF8 spreadsheet names etc.** — `detail` is `str(e)`, already safe.
- **The QUOTE pipeline** shares `extract_email_content`. It gets the structured
  result available but this phase does **not** change what it stores. Wiring the
  quote pipeline's own result is a follow-up (it has the same blindness).
- **Existing stored results** lack `input`. Anything reading it must tolerate
  absence — the modal already guards with `rd.warnings && rd.warnings.length`.
- **`EMPTY` on a scanned PDF** is common and benign; it must not read as an
  error in the warning copy.

---

## 6. Testing

New `tests/test_email_pipeline_failures.py`:

- each extractor maps its failure mode to the right code (mock
  `llm_call_with_retry` to raise → `MODEL_ERROR` with detail; return empty →
  `EMPTY`; feed a corrupt xlsx → `PARSE_ERROR`)
- success path returns `failure is None` and unchanged text
- `_extract_email_content_sync` still returns a plain `str` (compatibility)
- `extract_email_content` counts: `attachment_read` excludes signatures and
  failures; `failures` has one entry per failed attachment
- bundle-level: missing email → `EMAIL_NOT_FOUND`; no body and nothing readable
  → `NO_CONTENT`
- placeholder text is neutral — raw error text never appears in `bundle.text`

Extend `tests/tools/test_rfq_creation_pipeline.py`:

- a run where `extract_pdf_content` reports `MODEL_ERROR` stores
  `rfq_creation_result["input"]["failures"]` with the filename and code
- that same run appends exactly one human warning line
- a clean run stores `failures: []` and adds no warning

Existing suite must stay green (baseline **1441 passed / 23 skipped**); the 8
spreadsheet tests in `tests/test_email_pipeline.py` are updated for the new
return shape as part of this.

---

## 7. Files touched

| File | Change |
|---|---|
| `includes/email_pipeline.py` | **new** `AttachmentFailure`, `BundleFailure`, `AttachmentExtraction`, `ContentBundle`; three extractors return `AttachmentExtraction` |
| `includes/tools/supplier_quote_pipeline.py` | `_extract_email_content_sync` rewritten over `extract_email_content`; new `extract_email_content()`; placeholder swap |
| `includes/tools/rfq_creation_pipeline.py` | `_extract_rfq_items_sync` sets `llm_result["_input_report"]`; stage 4 stores `result["input"]` + appends the warning |
| `templates/partials/rfq_detail.html` | *(optional)* `rd.input` block in the pipeline modal |
| `templates/partials/admin_emails.html` | *(optional)* same block, same modal |
| `tests/test_email_pipeline.py` | 8 spreadsheet tests updated to the new return shape |
| `tests/test_email_pipeline_failures.py` | **new** |
| `tests/tools/test_rfq_creation_pipeline.py` | +3 cases |

No migration, no `models.py` change, no schema change.

---

## 8. Open questions — all resolved (2026-09-23)

1. **Warning copy / loudness** — the warning line in the comms modal now. The
   reviewer's intent is ultimately a **message on the RFQ itself**, deferred to
   §10 so the display is built once rather than twice.
2. **`unsupported` in the warning** — **keep it.** File types being skipped is
   exactly the thing the team needs visibility on, even though it will warn on
   every email carrying an `.eml`/`.zip`. Noise accepted deliberately.
3. **Should the QUOTE pipeline also record `input`?** — **Yes.** The improvement
   is meant to serve *any* process that reads emails/attachments, so the QUOTE
   pipeline was wired in the same pass (§11).
4. **Does one `model_error` force review regardless of score?** — **Yes**, for
   §10. A single unreadable attachment is not something a high score should be
   able to mask.
5. **`plan-pipelineReliability.prompt.md` is stale** — annotated as such (§11).

---

## 9. Implementation order

1. Taxonomies + `AttachmentExtraction`; three extractors return it; update the 8
   tests. Suite green.
2. `ContentBundle` + `extract_email_content()`; `_extract_email_content_sync`
   becomes a wrapper. Suite green (proves backward compatibility).
3. Bundle-level codes (`EMAIL_NOT_FOUND`, `NO_CONTENT`) replacing the
   `"Error: ..."` sentinels — **keep** the legacy strings on the
   `_extract_email_content_sync` wrapper so existing prefix checks still work.
4. Thread the report through `_extract_rfq_items_sync` → stage 4; store `input`;
   append the warning line.
5. New/updated tests; full suite.
6. *(Optional)* modal blocks.
7. Manual check against email #49663 — the original 10-attachment case — and
   confirm the summary reports `9 of 10` with `estimate QBRI1207.pdf` named.

---

## 10. Phase 2 preview — confidence (context only, not this plan)

For review context, so §3 is built compatibly. Copies the supplier-dedup shape
(`confidence: float` + `reasons: list[str]` + `tier: 'certain' | 'review'`, per
`scripts/scan_supplier_duplicates.py`).

**Inputs** — `input.failures` and
`attachment_read/attachment_total` (from this phase); the per-item
`confidence: high | medium | low` the extraction prompt **already returns and
`_add_items_sync` currently discards** (`config/prompts/rfq_creation_extract.md`);
items missing quantity/uom; any "missing items" the model reports; items with no
part number.

**Output** — an RFQ-level `{score, reasons[], tier}`; a per-line
`confidence` column on `rfq_items`; both surfaced on the items tab (the RFQ-page
display decision 6 defers).

**Why the order matters** — a score is only meaningful over a complete input.
Without §1, an attachment can vanish and the score will happily report high
confidence on the lines it happened to see.

---

## 11. As built (2026-09-23) — deviations from this plan

All six steps shipped. Two naming/structure deviations, both deliberate:

1. **`build_content_bundle`, not `extract_email_content`.** A nested agent tool
   already owns the name `extract_email_content`, so the bundle builder in
   `includes/tools/supplier_quote_pipeline.py` is `build_content_bundle()` —
   unambiguous at a call site and avoids shadowing the tool. §4/§9 above say
   `extract_email_content()`; read that as `build_content_bundle()`.
2. **Two modules, not one.** The `ContentBundle` dataclass (plus the failure
   enums and `AttachmentExtraction`) lives in `includes/email_pipeline.py`,
   beside the extractors that produce the failures. Only the *builder*
   (`build_content_bundle`) lives in `supplier_quote_pipeline.py`, because it
   needs `_get_email_tracking` and Gmail content backfill. §4 implied both in
   `email_pipeline.py`.

Also as built:

- Placeholders are `PLACEHOLDER_UNREADABLE` / `PLACEHOLDER_EMPTY` /
  `PLACEHOLDER_UNPARSED`; `detail` is truncated at `_DETAIL_MAX = 200` via
  `_short(exc)`. `AttachmentExtraction` has `.ok`.
- `_extract_rfq_items_sync` builds the bundle itself and defines a local
  `_fail(error, **extra)` so **every** early return (content missing, no prompt,
  LLM error, bad JSON, no items) carries `_input_report` and `warnings` — not
  just the happy path.
- The QUOTE pipeline (question 3) records `input` + `input_warnings` at its
  stage 2. Both templates render an "Attachments Read" block, amber only when
  `input.failures` is non-empty, and it shows the skipped-as-signature count.
- The `unsupported` code (question 2) warns, as decided.

**Tests** — `tests/test_email_pipeline_failures.py` (**new**, 22 cases:
`TestExtractorFailureCodes`, `TestContentBundleShape`, `TestBuildContentBundle`,
`TestLegacyWrapper`) plus 3 new cases in
`tests/tools/test_rfq_creation_pipeline.py::TestInputCompletenessRecording`.
Baseline moved 1441 → **1466 passed / 23 skipped**.

**Still unverified manually** — the plan's §9 step 7: replay email #49663 (the
original 10-attachment case) and confirm the summary reports `9 of 10` with
`estimate QBRI1207.pdf` named. Worth doing with
`scripts/test_rfq_creation.py --email-id 49663` before this is closed out.
