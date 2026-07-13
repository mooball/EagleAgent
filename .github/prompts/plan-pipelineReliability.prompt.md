# Plan: Email Pipeline Infrastructure — Shared LLM Helpers, Signature Detection & Retry

**Status:** Not started
**Created:** 2026-07-13
**Branch:** rfq-updates-july

## Problem

The supplier quote email pipeline (`includes/tools/supplier_quote_pipeline.py`) has a 95.3% success rate (223/234). The 7 DEADLINE_EXCEEDED failures break into two patterns:

1. **Stochastic timeouts on small inputs** — The thinking model (`gemini-2.5-flash`) sometimes burns 60+ seconds of "thinking" on trivial inputs, hitting Vertex AI's server-side 504 timeout.
2. **Large multi-attachment emails** — Emails with 7-10 inline images (many being corporate signature/logo images from email threads) produce enormous content bundles. Signature images waste LLM calls and inflate context.

Production data shows the same signature images (78KB, 370KB, 42KB, 36KB) appearing across multiple suppliers' emails — they're Eagle Exports' own email signature images embedded in quoted reply chains.

## Design Principle: Reusable Infrastructure

The supplier quote pipeline is the first of several email analysis pipelines:

| Pipeline | Purpose | Status |
|----------|---------|--------|
| **Supplier Quote** | Classify → extract → interpret supplier quote emails | Live (this plan improves it) |
| **Customer Request** | Classify → analyse → create RFQ from customer emails | Planned |
| **Customer Response** | Classify → interpret customer responses to our quotes | Planned |

All three share common needs: LLM calls with retry, image signature detection, attachment extraction (PDF/image/spreadsheet OCR). This plan extracts reusable components into a shared module so future pipelines don't duplicate logic.

### What's generic vs pipeline-specific

| Component | Location | Reused? |
|-----------|----------|---------|
| LLM call with retry + model fallback | `includes/email_pipeline.py` | All pipelines |
| Image signature cache (check/store/classify) | `includes/email_pipeline.py` | All pipelines |
| Attachment extraction (PDF, image, spreadsheet OCR) | `includes/email_pipeline.py` | All pipelines |
| `KnownImageSignature` model | `includes/dashboard/models.py` | All pipelines |
| Per-pipeline model config resolution | `includes/email_pipeline.py` | All pipelines |
| Classify prompt & logic | `supplier_quote_pipeline.py` | Supplier-specific |
| Interpret prompt & logic | `supplier_quote_pipeline.py` | Supplier-specific |
| Apply (write quote data to RFQ) | `supplier_quote_pipeline.py` | Supplier-specific |
| `trigger_supplier_quote_pipeline()` | `supplier_quote_pipeline.py` | Supplier-specific |

---

## Key Files

| File | Change |
|------|--------|
| `includes/email_pipeline.py` | **NEW** — shared LLM helpers, signature detection, attachment extraction |
| `includes/tools/supplier_quote_pipeline.py` | Refactor to import from `email_pipeline`, remove duplicated code |
| `config/settings.py` | Add per-pipeline-step model config pattern |
| `includes/dashboard/models.py` | Add `KnownImageSignature` model |
| `alembic/versions/` | Migration for new table |

---

## Step 1: Create `includes/email_pipeline.py` — Shared Infrastructure

Create a new file `includes/email_pipeline.py` containing all reusable components. This module has no knowledge of RFQs, suppliers, or quotes — it deals only with emails, LLM calls, images, and attachments.

### 1a. Imports and constants

```python
"""Shared email pipeline infrastructure.

Reusable components for email analysis pipelines:
- LLM call with retry and model fallback
- Image signature detection and caching
- Attachment content extraction (PDF, image, spreadsheet)

Pipeline-specific logic (prompts, interpretation, DB writes) stays in
each pipeline's own module (e.g. supplier_quote_pipeline.py).
"""

import base64
import hashlib
import json
import logging
import time
from typing import Optional

from config.settings import Config

logger = logging.getLogger(__name__)

# Default fallback model — fast, non-thinking, reliable
FALLBACK_MODEL = "gemini-2.0-flash"
```

### 1b. Model resolution

A generic model resolver that supports any pipeline with any number of steps. Each pipeline registers its config keys; the resolver checks step-specific → pipeline-level → global default.

```python
def get_pipeline_model(pipeline: str, step: str) -> str:
    """Resolve the LLM model for a pipeline step.

    Resolution order:
      1. Step-specific env var: {PIPELINE}_{STEP}_MODEL (e.g. QUOTE_CLASSIFY_MODEL)
      2. Pipeline-level env var: {PIPELINE}_PIPELINE_MODEL (e.g. QUOTE_PIPELINE_MODEL)
      3. Config.DEFAULT_MODEL

    Args:
        pipeline: Pipeline name prefix, e.g. "QUOTE", "CUSTOMER_REQUEST"
        step: Step name, e.g. "classify", "extract", "interpret"
    """
    import os
    # Step-specific: QUOTE_CLASSIFY_MODEL
    step_var = f"{pipeline}_{step.upper()}_MODEL"
    step_model = os.getenv(step_var, "")
    if step_model:
        return step_model

    # Pipeline-level: QUOTE_PIPELINE_MODEL
    pipeline_var = f"{pipeline}_PIPELINE_MODEL"
    pipeline_model = os.getenv(pipeline_var, "")
    if pipeline_model:
        return pipeline_model

    return Config.DEFAULT_MODEL
```

This is fully env-var-driven so new pipelines don't require config/settings.py changes — just set env vars like `CUSTOMER_REQUEST_CLASSIFY_MODEL`.

### 1c. LLM call with retry

```python
def llm_call_with_retry(
    pipeline: str,
    step: str,
    contents,
    temperature: float = 0.1,
    timeout: int = 120000,
) -> "GenerateContentResponse":
    """Call Gemini with retry on transient errors.

    Retry strategy:
      1. Try primary model for the pipeline/step
      2. Retry primary model once (transient 504s)
      3. Fall back to FALLBACK_MODEL

    Args:
        pipeline: Pipeline name prefix (e.g. "QUOTE")
        step: Step name (e.g. "classify", "extract", "interpret")
        contents: Gemini contents (string, list of Parts, etc.)
        temperature: LLM temperature
        timeout: HTTP timeout in milliseconds

    Returns the response object. Raises on permanent failure after all retries.
    """
    from google import genai as _genai
    from google.genai import types as _types

    primary_model = get_pipeline_model(pipeline, step)
    models_to_try = [primary_model, primary_model, FALLBACK_MODEL]

    last_error = None
    for attempt, model in enumerate(models_to_try):
        try:
            client = _genai.Client(http_options={"timeout": timeout})
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=_types.GenerateContentConfig(temperature=temperature),
            )
            if attempt > 0:
                print(
                    f"[email-pipeline] {pipeline}/{step}: succeeded on "
                    f"attempt {attempt + 1} (model={model})",
                    flush=True,
                )
            return response
        except Exception as e:
            error_str = str(e)
            is_transient = any(
                code in error_str
                for code in (
                    "504", "503", "DEADLINE_EXCEEDED",
                    "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                )
            )
            if not is_transient:
                raise  # permanent error — don't retry
            last_error = e
            print(
                f"[email-pipeline] {pipeline}/{step}: attempt {attempt + 1} "
                f"failed (model={model}): {e}",
                flush=True,
            )
            logger.warning(
                f"[email-pipeline] {pipeline}/{step}: attempt {attempt + 1} "
                f"failed (model={model}): {e}"
            )
            if attempt < len(models_to_try) - 1:
                time.sleep(2 ** attempt)  # 1s, 2s backoff

    raise last_error  # all retries exhausted
```

### 1d. Image signature detection

```python
def _get_session():
    """Get a synchronous SQLAlchemy session."""
    from includes.dashboard.database import get_session
    return get_session()


def check_image_signature(image_bytes: bytes) -> str | None:
    """Check if image bytes match a known signature.

    Returns 'signature' or 'quote_content' if known, None if unknown.
    """
    sha = hashlib.sha256(image_bytes).hexdigest()
    session = _get_session()
    try:
        from includes.dashboard.models import KnownImageSignature
        record = session.query(KnownImageSignature).filter(
            KnownImageSignature.sha256 == sha
        ).first()
        return record.classification if record else None
    finally:
        session.close()


def store_image_signature(
    image_bytes: bytes,
    classification: str,
    filename: str | None = None,
    source_email_id: int | None = None,
) -> None:
    """Store image hash and classification for future lookups."""
    sha = hashlib.sha256(image_bytes).hexdigest()
    session = _get_session()
    try:
        from includes.dashboard.models import KnownImageSignature
        existing = session.query(KnownImageSignature).filter(
            KnownImageSignature.sha256 == sha
        ).first()
        if existing:
            return  # already known
        from datetime import datetime, timezone
        record = KnownImageSignature(
            sha256=sha,
            classification=classification,
            sample_filename=filename,
            source_email_id=source_email_id,
            size_bytes=len(image_bytes),
            created_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning(f"Failed to store image signature: {e}")
    finally:
        session.close()


def classify_image_content(
    image_bytes: bytes,
    mime_type: str,
    pipeline: str = "QUOTE",
) -> str:
    """Ask the LLM whether an image is a signature/logo or meaningful content.

    Returns 'signature' or 'quote_content'.
    Uses the extract model for the given pipeline.
    """
    from google.genai import types as _types

    try:
        response = llm_call_with_retry(
            pipeline=pipeline,
            step="extract",
            contents=[
                _types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                (
                    "Is this image a corporate logo, email signature, banner, "
                    "social media icon, or decorative element? Or does it contain "
                    "meaningful content like a quotation, price list, invoice, "
                    "product specifications, or tabular data?\n\n"
                    "Reply with ONLY one word: 'signature' or 'content'"
                ),
            ],
            temperature=0.0,
            timeout=15000,
        )
        answer = (response.text or "").strip().lower()
        if "signature" in answer:
            return "signature"
        return "quote_content"
    except Exception as e:
        logger.warning(f"Image classification failed, assuming quote_content: {e}")
        return "quote_content"  # err on side of inclusion


def triage_image(
    image_bytes: bytes,
    mime_type: str,
    filename: str | None = None,
    source_email_id: int | None = None,
    pipeline: str = "QUOTE",
) -> str:
    """Full image triage: check cache → classify if unknown → cache result.

    Returns 'signature' or 'quote_content'.
    """
    # 1. Check cache
    cached = check_image_signature(image_bytes)
    if cached is not None:
        if cached == "signature":
            print(f"[email-pipeline] Skipping known signature: {filename}", flush=True)
        return cached

    # 2. Classify unknown image
    classification = classify_image_content(image_bytes, mime_type, pipeline)

    # 3. Cache for future
    store_image_signature(image_bytes, classification, filename, source_email_id)
    if classification == "signature":
        print(f"[email-pipeline] New signature detected & cached: {filename}", flush=True)
    return classification
```

### 1e. Attachment extraction functions

Move these three functions **out of** `supplier_quote_pipeline.py` and into `email_pipeline.py`. They are already generic — they have no supplier/quote-specific logic.

```python
def extract_pdf_content(
    pdf_bytes: bytes,
    filename: str,
    pipeline: str = "QUOTE",
) -> str:
    """Extract text and tabular data from a PDF via Gemini."""
    from google.genai import types as _types

    try:
        response = llm_call_with_retry(
            pipeline=pipeline,
            step="extract",
            contents=[
                _types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                (
                    "Extract ALL text, pricing information, part numbers, quantities, "
                    "and tabular data from this document. Present the data as Markdown "
                    "tables. Include:\n"
                    "- Item descriptions and part numbers\n"
                    "- Unit prices and totals with currency\n"
                    "- Quantities and units of measure\n"
                    "- Shipping costs, lead times, payment terms if mentioned\n"
                    "- Any notes or conditions\n\n"
                    "If the document contains multiple tables, reproduce each one. "
                    "Preserve the original structure as faithfully as possible."
                ),
            ],
            temperature=0.1,
            timeout=120000,
        )
        if not response.text:
            candidates = getattr(response, "candidates", None)
            if candidates and candidates[0].finish_reason:
                logger.warning(
                    f"Gemini PDF extraction empty for {filename}: "
                    f"finish_reason={candidates[0].finish_reason}"
                )
            return "*[No content extracted from PDF]*"
        return response.text
    except Exception as e:
        logger.error(f"Gemini PDF extraction failed for {filename}: {e}")
        return f"*[PDF extraction failed: {e}]*"


def extract_image_content(
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    pipeline: str = "QUOTE",
) -> str:
    """Extract text and tabular data from an image via Gemini OCR."""
    from google.genai import types as _types

    try:
        response = llm_call_with_retry(
            pipeline=pipeline,
            step="extract",
            contents=[
                _types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                (
                    "Extract all text, pricing, and tabular data from this image. "
                    "If it's a quotation or price list, present as a Markdown table. "
                    "If it's a product spec sheet, extract key specifications. "
                    "If it's general text, reproduce it faithfully."
                ),
            ],
            temperature=0.1,
            timeout=60000,
        )
        return response.text or "*[No content extracted]*"
    except Exception as e:
        logger.error(f"Gemini image extraction failed for {filename}: {e}")
        return f"*[Image extraction failed: {e}]*"


def extract_spreadsheet_content(data: bytes, filename: str, mime_type: str) -> str:
    """Extract spreadsheet content. CSV parsed directly; Excel via openpyxl.

    No LLM call — purely local parsing.
    """
    if filename.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8", errors="replace")
            return f"```csv\n{text[:5000]}\n```"
        except Exception:
            pass

    try:
        import io
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            parts.append(f"## Sheet: {sheet_name}\n")
            header = rows[0]
            col_names = [str(c) if c is not None else "" for c in header]
            parts.append("| " + " | ".join(col_names) + " |")
            parts.append("| " + " | ".join(["---"] * len(col_names)) + " |")
            for row in rows[1:200]:
                cells = [str(c) if c is not None else "" for c in row]
                parts.append("| " + " | ".join(cells) + " |")
        wb.close()
        result_text = "\n".join(parts)
        if not result_text.strip():
            return "*[Spreadsheet appears empty]*"
        return result_text[:8000]
    except Exception as e:
        logger.error(f"Local spreadsheet extraction failed for {filename}: {e}")
        return f"*[Spreadsheet extraction failed: {e}]*"
```

### 1f. Gmail attachment fetch helper

Move `_fetch_gmail_attachment_bytes()` from `supplier_quote_pipeline.py` into `email_pipeline.py` — it's generic.

```python
def fetch_gmail_attachment_bytes(
    user_email: str, message_id: str, attachment_id: str
) -> bytes | None:
    """Fetch raw attachment bytes from Gmail API."""
    try:
        from includes.gmail import get_gmail_client
        service = get_gmail_client(user_email)
        attachment = (
            service.users().messages().attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(attachment["data"])
    except Exception as e:
        logger.warning(f"Failed to fetch attachment {message_id}/{attachment_id}: {e}")
        return None
```

---

## Step 2: Add `KnownImageSignature` Model & Migration

### 2a. SQLAlchemy model

Add to `includes/dashboard/models.py`, after the `MailboxSyncCursor` class (around line 345):

```python
class KnownImageSignature(Base):
    """Cache of image checksums for skipping known signature/logo images in email pipelines."""
    __tablename__ = 'known_image_signatures'

    sha256 = Column(String(64), primary_key=True)  # hex-encoded SHA-256
    classification = Column(String, nullable=False)  # 'signature' or 'quote_content'
    sample_filename = Column(String, nullable=True)  # first filename seen
    source_email_id = Column(Integer, nullable=True)  # email_tracking.id where first seen
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<KnownImageSignature(sha256='{self.sha256[:12]}...', classification='{self.classification}')>"
```

### 2b. Alembic migration

Create `alembic/versions/m9n0p1q2r3s4_add_known_image_signatures.py`:

```python
"""add known_image_signatures table

Revision ID: m9n0p1q2r3s4
Revises: l8m9n0p1q2r3
Create Date: 2026-07-13 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'm9n0p1q2r3s4'
down_revision = 'l8m9n0p1q2r3'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'known_image_signatures',
        sa.Column('sha256', sa.String(64), primary_key=True),
        sa.Column('classification', sa.String(), nullable=False),
        sa.Column('sample_filename', sa.String(), nullable=True),
        sa.Column('source_email_id', sa.Integer(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )

def downgrade():
    op.drop_table('known_image_signatures')
```

---

## Step 3: Refactor `supplier_quote_pipeline.py` to Use Shared Module

### 3a. Remove functions that moved to `email_pipeline.py`

Delete these functions from `supplier_quote_pipeline.py` (they now live in `email_pipeline.py`):

- `_fetch_gmail_attachment_bytes()` — replaced by `email_pipeline.fetch_gmail_attachment_bytes()`
- `_extract_pdf_with_gemini()` — replaced by `email_pipeline.extract_pdf_content()`
- `_extract_image_with_gemini()` — replaced by `email_pipeline.extract_image_content()`
- `_extract_spreadsheet_with_gemini()` — replaced by `email_pipeline.extract_spreadsheet_content()`

### 3b. Remove the `_PIPELINE_MODEL` constant

Delete this line near the top of the file:
```python
_PIPELINE_MODEL = Config.QUOTE_PIPELINE_MODEL or Config.DEFAULT_MODEL
```

It is replaced by `email_pipeline.get_pipeline_model("QUOTE", step)`.

### 3c. Add imports from the shared module

Add near the top of `supplier_quote_pipeline.py`:

```python
from includes.email_pipeline import (
    llm_call_with_retry,
    get_pipeline_model,
    fetch_gmail_attachment_bytes,
    extract_pdf_content,
    extract_image_content,
    extract_spreadsheet_content,
    triage_image,
)
```

Remove the `base64` import (no longer needed here — it moved to `email_pipeline.py`).

### 3d. Update `_classify_supplier_email_sync()`

Replace the existing LLM call block. Currently:
```python
from google import genai as _genai
from google.genai import types as _types

client = _genai.Client(http_options={"timeout": 30000})
response = client.models.generate_content(
    model=_PIPELINE_MODEL,
    contents=[...],
    config=_types.GenerateContentConfig(temperature=0.0),
)
```

Replace with:
```python
from google.genai import types as _types

response = llm_call_with_retry(
    pipeline="QUOTE",
    step="classify",
    contents=[f"{_SUPPLIER_CLASSIFY_PROMPT}\n\n---\n\n{email_context}"],
    temperature=0.0,
    timeout=30000,
)
```

Keep the existing empty-response check and JSON parsing. Keep the heuristic fallback in the outer `except` block — it catches permanent failures after retry exhaustion.

Update the two logging lines that reference `_PIPELINE_MODEL` to use `get_pipeline_model("QUOTE", "classify")`.

### 3e. Update `_extract_email_content_sync()`

This function currently calls the local extraction helpers. Update to use the shared module functions and add image triage.

Replace the attachment processing loop's dispatch section. Currently:
```python
if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
    extracted = _extract_pdf_with_gemini(raw_bytes, filename)
    parts.append(f"{header}\n\n{extracted}")
elif mime_type.startswith("image/"):
    extracted = _extract_image_with_gemini(raw_bytes, filename, mime_type)
    parts.append(f"{header}\n\n{extracted}")
elif "spreadsheet" in mime_type or filename.lower().endswith((".xlsx", ".xls", ".csv")):
    extracted = _extract_spreadsheet_with_gemini(raw_bytes, filename, mime_type)
    parts.append(f"{header}\n\n{extracted}")
else:
    parts.append(f"{header}\n\n*[Unsupported attachment type: {mime_type}]*")
```

Replace with:
```python
if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
    extracted = extract_pdf_content(raw_bytes, filename, pipeline="QUOTE")
    parts.append(f"{header}\n\n{extracted}")
elif mime_type.startswith("image/"):
    # Triage: skip known signatures, classify unknowns
    if triage_image(raw_bytes, mime_type, filename, email_tracking_id, pipeline="QUOTE") == "signature":
        continue
    extracted = extract_image_content(raw_bytes, filename, mime_type, pipeline="QUOTE")
    parts.append(f"{header}\n\n{extracted}")
elif "spreadsheet" in mime_type or filename.lower().endswith((".xlsx", ".xls", ".csv")):
    extracted = extract_spreadsheet_content(raw_bytes, filename, mime_type)
    parts.append(f"{header}\n\n{extracted}")
else:
    parts.append(f"{header}\n\n*[Unsupported attachment type: {mime_type}]*")
```

Also update the `_fetch_gmail_attachment_bytes` call to use the shared version:
```python
raw_bytes = fetch_gmail_attachment_bytes(
    tracking.user_email, tracking.gmail_message_id, att_id
)
```

Also **remove the `quote_attachments` filename-based filtering logic** — the triage_image approach replaces it. Remove these lines:
```python
# Build set of filenames to extract (case-insensitive match)
target_filenames = None
if quote_attachments:
    target_filenames = {f.lower().strip() for f in quote_attachments}
```
And the `if target_filenames is not None:` / `if filename.lower().strip() not in target_filenames:` / `continue` block inside the attachment loop.

Instead, **process all attachments** (both inline and non-inline) — the image triage handles skipping signatures. Remove the `if att.get("inline"): continue` fallback too.

The function signature stays the same for backward compatibility, but the `quote_attachments` parameter becomes unused. Update the docstring to note this:
```python
def _extract_email_content_sync(email_tracking_id: int, quote_attachments: list[str] | None = None) -> str:
    """Synchronous extraction: fetch body + process attachments.

    Image attachments are triaged via the signature cache — known
    signature/logo images are skipped automatically. The quote_attachments
    parameter is deprecated and ignored (kept for API compatibility).
    """
```

### 3f. Update `_interpret_quote_sync()`

Replace the existing LLM call. Currently:
```python
client = _genai.Client(http_options={"timeout": 120000})
response = client.models.generate_content(
    model=_PIPELINE_MODEL,
    contents=prompt,
    config=_types.GenerateContentConfig(temperature=0.1),
)
```

Replace with:
```python
response = llm_call_with_retry(
    pipeline="QUOTE",
    step="interpret",
    contents=prompt,
    temperature=0.1,
    timeout=300000,  # 5 min — interpret can be slow for large RFQs
)
```

Remove the `from google import genai as _genai` and `from google.genai import types as _types` imports from this function (they're no longer needed here).

Keep the existing empty-response check and JSON parsing after the call.

---

## Step 4: Clean Up Config

### 4a. Remove `QUOTE_PIPELINE_MODEL` from `config/settings.py`

The existing line:
```python
QUOTE_PIPELINE_MODEL = os.getenv("QUOTE_PIPELINE_MODEL", "")
```

Keep it for backward compatibility — `get_pipeline_model()` reads it directly from `os.getenv()` so the Config attribute isn't strictly needed, but keeping it documents the variable and lets the `print_config()` method show it.

No new config lines are needed — `get_pipeline_model()` reads env vars dynamically based on pipeline+step names.

### 4b. Remove `Config.QUOTE_PIPELINE_MODEL` reference

In `supplier_quote_pipeline.py`, remove:
```python
from config.settings import Config
```
if it was only used for `Config.QUOTE_PIPELINE_MODEL`. Check whether other `Config` references remain (e.g. `Config.DEFAULT_MODEL` is no longer needed since model resolution is in `email_pipeline.py`).

Actually — `Config` may still be imported for other purposes in the file. Only remove it if no other references remain after the refactor.

---

## Summary of Changes

| File | Change |
|------|--------|
| `includes/email_pipeline.py` | **NEW** — `get_pipeline_model()`, `llm_call_with_retry()`, `check_image_signature()`, `store_image_signature()`, `classify_image_content()`, `triage_image()`, `fetch_gmail_attachment_bytes()`, `extract_pdf_content()`, `extract_image_content()`, `extract_spreadsheet_content()` |
| `includes/tools/supplier_quote_pipeline.py` | Remove `_PIPELINE_MODEL`, `_fetch_gmail_attachment_bytes`, `_extract_pdf_with_gemini`, `_extract_image_with_gemini`, `_extract_spreadsheet_with_gemini`. Import from `email_pipeline`. Update LLM calls to use `llm_call_with_retry()`. Add image triage in extract. Remove filename-based filtering. |
| `includes/dashboard/models.py` | Add `KnownImageSignature` model |
| `alembic/versions/m9n0p1q2r3s4_...py` | Migration for `known_image_signatures` table |

## Testing

1. Run `uv run pytest tests/ -x --timeout=60 -q --no-header` — all existing tests must pass
2. Manually re-run the pipeline for email #24633 (Sika) via the dashboard "Run" button — should succeed with retry
3. Re-run email #24506 (AAA Imports, 10 inline images) — should skip most as signatures and succeed
4. Check `known_image_signatures` table after runs — verify signatures are being cached
5. Re-run a previously-processed email — verify cached signatures are hit (no LLM classify calls)

## Deployment

1. Merge and deploy
2. Run `alembic upgrade head` on Railway to create the `known_image_signatures` table
3. The signature DB is self-populating — no backfill. First encounter of each image triggers one cheap LLM classify call (~0.5s), then it's cached permanently
4. Optionally set per-step models in Railway env vars:
   ```
   QUOTE_CLASSIFY_MODEL=gemini-2.5-flash
   QUOTE_EXTRACT_MODEL=gemini-2.0-flash
   QUOTE_INTERPRET_MODEL=gemini-2.0-flash
   ```

## Future Pipelines

When building the Customer Request or Customer Response pipelines, they will:
1. Import from `includes.email_pipeline` for LLM calls, image triage, and attachment extraction
2. Define their own classify/interpret prompts and apply logic
3. Use `pipeline="CUSTOMER_REQUEST"` or `pipeline="CUSTOMER_RESPONSE"` in all shared function calls
4. Get per-step model config for free via env vars (e.g. `CUSTOMER_REQUEST_CLASSIFY_MODEL`)
5. Share the same `known_image_signatures` cache — a signature learned from supplier emails is automatically skipped in customer emails too
