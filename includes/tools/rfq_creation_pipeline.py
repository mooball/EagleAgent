"""RFQ creation pipeline — extract items from customer request emails and create RFQs.

Two-stage pipeline (after guard checks):
  1. Create RFQ & link to email immediately (user can navigate between systems)
  2. Extract line items + notes via LLM from email content bundle

Trigger: manual — user clicks "Create RFQ" in the Gmail add-on or dashboard.
Automated classification (Stage 0) deferred to future phase.

LLM calls are delegated to the shared includes/email_pipeline module.
"""

import json
import logging
import threading
from typing import Optional

from includes.email_pipeline import (
    llm_call_with_retry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_session():
    """Get a synchronous SQLAlchemy session."""
    from includes.dashboard.database import get_session
    return get_session()


def _get_email_tracking(session, email_tracking_id: int):
    """Fetch an EmailTracking record by ID."""
    from includes.dashboard.models import EmailTracking
    return session.query(EmailTracking).filter(EmailTracking.id == email_tracking_id).first()


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_dt():
    """Return current UTC datetime."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _save_rfq_creation_result(email_tracking_id: int, result: dict) -> None:
    """Persist pipeline result to the email_tracking record."""
    session = _get_session()
    try:
        tracking = _get_email_tracking(session, email_tracking_id)
        if not tracking:
            logger.warning(f"[rfq-creation] #{email_tracking_id}: cannot save result — email not found")
            return
        tracking.rfq_creation_result = result
        session.commit()
        logger.info(f"[rfq-creation] #{email_tracking_id}: result saved "
                    f"(status={result.get('status', '?')}, items={result.get('items_extracted', 0)})")
    except Exception as e:
        session.rollback()
        logger.warning(f"[rfq-creation] #{email_tracking_id}: failed to save result — {e}")
    finally:
        session.close()


def _save_error(email_tracking_id: int, error: str) -> None:
    """Save an error result."""
    _save_rfq_creation_result(email_tracking_id, {
        "status": "error",
        "error": error,
        "processed_at": _now_iso(),
    })


def _deduplicate_items(items: list) -> list:
    """Deduplicate items by (description + input_code)."""
    seen = set()
    deduped = []
    for item in items:
        key = (item.get("input_description", "").strip().lower(),
               item.get("input_code", "").strip().lower() or item.get("brand", "").strip().lower())
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# Trigger (public entry point)
# ---------------------------------------------------------------------------

def trigger_rfq_creation_pipeline(email_tracking_id: int, user_id: str = "system") -> None:
    """Run the RFQ creation pipeline for an email.

    Called from:
      - POST /api/addon/create-rfq (Gmail add-on)
      - Dashboard "Create RFQ" button (future)

    Runs in a background daemon thread to avoid blocking the caller.
    Failures are logged but don't propagate.
    """
    def _run() -> None:
        try:
            logger.info(f"[rfq-creation] #{email_tracking_id}: thread started")
            _run_rfq_creation_pipeline(email_tracking_id, user_id)
        except Exception:
            logger.exception(f"[rfq-creation] #{email_tracking_id}: unhandled error")
            try:
                _save_error(email_tracking_id, "Pipeline crashed — check server logs")
            except Exception:
                pass

    thread = threading.Thread(
        target=_run,
        daemon=True,
        name=f"rfq-creation-{email_tracking_id}",
    )
    thread.start()
    logger.info(f"[rfq-creation] #{email_tracking_id}: daemon thread spawned")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _run_rfq_creation_pipeline(email_tracking_id: int, user_id: str) -> None:
    """Execute the full RFQ creation pipeline."""

    # ---------- Guard Checks ----------
    session = _get_session()
    try:
        tracking = _get_email_tracking(session, email_tracking_id)
        if not tracking:
            _save_error(email_tracking_id, "Email not found")
            return

        # Must be linked to a customer (UI enforces this too)
        if not tracking.customer_id:
            _save_error(email_tracking_id, "No customer linked to this email. Link a customer first.")
            return

        # Skip if RFQ already created (e.g., synchronously by the addon endpoint).
        # Don't log as an error — this is an expected path.
        if tracking.rfq_token or tracking.rfq_id:
            logger.info(
                f"[rfq-creation] #{email_tracking_id}: RFQ already linked "
                f"({tracking.rfq_token or tracking.rfq_id}), skipping"
            )
            return

        # Idempotency: skip if already processed
        if tracking.rfq_creation_result:
            logger.info(f"[rfq-creation] #{email_tracking_id}: already processed, skipping")
            return

        # Direction NOT checked — manual trigger means user decided this IS a quote request
        logger.info(f"[rfq-creation] #{email_tracking_id}: guard checks passed "
                    f"(customer={tracking.customer_id}, direction={tracking.direction})")

        # Capture data needed after session close
        customer_id = tracking.customer_id
        user_email = tracking.user_email
        subject = tracking.subject or ""
        gmail_thread_id = tracking.gmail_thread_id

    finally:
        session.close()

    # ---------- Stage 2: Create RFQ & link to email ----------
    from includes.tools.rfq_crud import _create_rfq_sync
    from includes.dashboard.models import Customer
    from sqlalchemy import text

    session = _get_session()
    try:
        customer = session.query(Customer).get(customer_id)
        customer_name = customer.companyname if customer else "Unknown"

        rfq = _create_rfq_sync(
            data={
                "customer": customer_name,
                "customer_id": str(customer.id),
                "status": "in_progress",
                "assigned_to": user_email,
                "reference": subject,
            },
            user_id=user_id,
        )
        if isinstance(rfq, str):
            _save_error(email_tracking_id, rfq)
            return

        rfq_number = rfq["rfq_number"]
        logger.info(f"[rfq-creation] #{email_tracking_id}: created {rfq_number} for {customer_name}")

        # Link entire email thread to the new RFQ immediately
        session.execute(
            text(
                "UPDATE email_tracking SET rfq_token = :token, match_type = 'manual' "
                "WHERE gmail_thread_id = :tid"
            ),
            {"token": rfq_number, "tid": gmail_thread_id},
        )
        session.commit()
        logger.info(f"[rfq-creation] #{email_tracking_id}: linked email thread to {rfq_number}")

        # Auto-create NetSuite Opportunity if customer has a netsuite_id
        if customer.netsuite_id:
            try:
                from includes.netsuite.records.opportunity import create_and_link_opportunity
                opp_result = create_and_link_opportunity(rfq_number)
                if opp_result.success:
                    logger.info(
                        f"[rfq-creation] #{email_tracking_id}: auto-created opportunity {opp_result.tran_id}"
                    )
                else:
                    logger.info(
                        f"[rfq-creation] #{email_tracking_id}: skipped opportunity creation — {opp_result.error}"
                    )
            except Exception as e:
                logger.warning(
                    f"[rfq-creation] #{email_tracking_id}: opportunity creation failed (non-fatal): {e}"
                )

    except Exception as e:
        session.rollback()
        logger.exception(f"[rfq-creation] #{email_tracking_id}: failed to create RFQ")
        _save_error(email_tracking_id, f"Failed to create RFQ: {e}")
        return
    finally:
        session.close()

    # ---------- Stage 3: Extract line items + notes (LLM) ----------
    try:
        items, llm_result = _extract_rfq_items_sync(email_tracking_id)
    except Exception as e:
        logger.exception(f"[rfq-creation] #{email_tracking_id}: extraction failed")
        # RFQ already exists and is linked — user can add items manually
        _save_rfq_creation_result(email_tracking_id, {
            "rfq_number": rfq_number,
            "items_extracted": 0,
            "customer": customer_name,
            "status": "partial",
            "extraction_method": "gemini_llm",
            "raw_items": [],
            "warnings": [f"Extraction failed: {e}"],
            "actions": [f"Created empty RFQ {rfq_number} (extraction failed)"],
            "processed_at": _now_iso(),
        })
        return

    # Add items to the RFQ
    if items:
        from includes.tools.rfq_crud import _add_items_sync
        try:
            _add_items_sync(
                rfq_number=rfq_number,
                data={"items": items},
                user_id=user_id,
            )
            logger.info(f"[rfq-creation] #{email_tracking_id}: added {len(items)} items to {rfq_number}")
        except Exception as e:
            logger.warning(f"[rfq-creation] #{email_tracking_id}: failed to add items — {e}")

    # Update RFQ title and notes from LLM extraction
    if llm_result:
        from includes.tools.rfq_crud import _update_rfq_sync
        updates = {}
        if llm_result.get("title"):
            updates["title"] = llm_result["title"]
        if llm_result.get("customer_notes"):
            updates["notes"] = llm_result["customer_notes"]
        if updates:
            try:
                _update_rfq_sync(rfq_number, updates, user_id)
                logger.info(f"[rfq-creation] #{email_tracking_id}: updated title/notes on {rfq_number}")
            except Exception as e:
                logger.warning(f"[rfq-creation] #{email_tracking_id}: failed to update title/notes — {e}")

    # ---------- Stage 4: Save result ----------
    result = {
        "rfq_number": rfq_number,
        "items_extracted": len(items),
        "customer": customer_name,
        "status": "complete",
        "extraction_method": "gemini_llm",
        "title": llm_result.get("title", "") if llm_result else "",
        "customer_notes": llm_result.get("customer_notes", "") if llm_result else "",
        "raw_items": items,
        "warnings": llm_result.get("warnings", []) if llm_result else [],
        "actions": [f"Created RFQ {rfq_number} with {len(items)} items"],
        "processed_at": _now_iso(),
    }

    # Include extraction diagnostics for debugging
    if llm_result:
        if llm_result.get("_raw_response"):
            result["llm_raw_response"] = llm_result["_raw_response"]
        if llm_result.get("error"):
            result["extraction_error"] = llm_result["error"]
            if llm_result.get("raw_response"):
                result["llm_raw_response"] = llm_result["raw_response"]
            result["status"] = "error"
        elif not llm_result.get("has_items", True):
            result["warnings"].append(
                f"LLM found no items (has_items=false). Raw response: "
                f"{llm_result.get('_raw_response', '')[:500]}"
            )

    if not items and result["status"] != "error":
        result["warnings"].append("No items could be extracted from the email content. "
                                  "RFQ created as empty draft — add items manually.")

    _save_rfq_creation_result(email_tracking_id, result)


# ---------------------------------------------------------------------------
# Stage 3: Item extraction (LLM)
# ---------------------------------------------------------------------------

# Lazy-loaded from config/prompts/rfq_creation_extract.md
_EXTRACTION_PROMPT: Optional[str] = None


def _load_extraction_prompt() -> str:
    """Load the extraction prompt from the config file (cached)."""
    global _EXTRACTION_PROMPT
    if _EXTRACTION_PROMPT is not None:
        return _EXTRACTION_PROMPT

    from pathlib import Path
    prompt_path = Path(__file__).parent.parent.parent / "config" / "prompts" / "rfq_creation_extract.md"
    try:
        _EXTRACTION_PROMPT = prompt_path.read_text()
        logger.info(f"[rfq-creation] loaded extraction prompt ({len(_EXTRACTION_PROMPT)} chars)")
    except FileNotFoundError:
        logger.warning(f"[rfq-creation] prompt file not found: {prompt_path}, using default")
        _EXTRACTION_PROMPT = _DEFAULT_EXTRACTION_PROMPT
    return _EXTRACTION_PROMPT


_DEFAULT_EXTRACTION_PROMPT = """\
You are analyzing a customer email to extract line items for a Request for Quote (RFQ).

## Instructions
Extract all line items from the email content. For each item, provide:
- `input_description`: the item description as written by the customer
- `input_code`: part number, SKU, or code if provided
- `brand`: manufacturer or brand if specified
- `quantity`: numeric quantity requested
- `uom`: unit of measure (ea, m, kg, etc.) — default to "ea" if not specified
- `confidence`: "high", "medium", or "low"

Also provide:
- `warnings`: list of genuinely problematic items (quantity missing entirely, ambiguous
  references like "same as last order"). DO NOT warn about missing part codes or brands —
  many items are adequately described by description alone (e.g. "M16 bolts").
- `title`: a concise description of what the RFQ is about (e.g. "Komatsu PC200 engine parts",
  "Hydraulic fittings and hoses"). Derived from the overall theme, not the email subject.
- `customer_notes`: any customer requirements, delivery dates, conditions, or context that
  applies to the whole request (e.g. "Required by 15 August", "Genuine OEM only").
  Set to empty string if no relevant requirements found.
- `has_items`: true if any items were found, false if this is a general enquiry.

Return ONLY valid JSON in this format:
```json
{
  "items": [
    {"input_description": "M16 bolt grade 8.8", "input_code": "M16-8.8", "brand": "", "quantity": 100, "uom": "ea", "confidence": "high"}
  ],
  "warnings": [],
  "title": "M16 fasteners",
  "customer_notes": "Must be grade 8.8 or higher",
  "has_items": true
}
```
"""


def _extract_rfq_items_sync(email_tracking_id: int) -> tuple[list, Optional[dict]]:
    """Extract line items and notes from email content via LLM.

    Returns (items, llm_result) where items is a list of dicts with
    {input_description, input_code, brand, quantity, uom, confidence}
    and llm_result is the full parsed LLM response dict.
    """
    from includes.tools.supplier_quote_pipeline import _extract_email_content_sync

    # Build content bundle (email body + PDF/image attachments + spreadsheets)
    # NOTE: internally uses QUOTE pipeline models for vision/PDF processing
    content_bundle = _extract_email_content_sync(email_tracking_id)
    if not content_bundle or content_bundle.startswith("Error:"):
        logger.warning(f"[rfq-creation] #{email_tracking_id}: empty content bundle — {content_bundle}")
        return [], {"error": f"Failed to extract email content: {content_bundle}"}

    # LLM extraction
    prompt = _load_extraction_prompt()
    full_prompt = f"{prompt}\n\n---\n\n## Email Content\n\n{content_bundle}"

    response = llm_call_with_retry(
        pipeline="RFQ_CREATION",
        step="extract",
        contents=full_prompt,
        temperature=0.1,
        timeout=120000,
    )

    raw_text = (response.text or "").strip()
    if not raw_text:
        logger.warning(f"[rfq-creation] #{email_tracking_id}: LLM returned empty response")
        return [], {"error": "LLM returned empty response", "raw_response": ""}

    # Parse JSON from response (handle markdown code fences)
    raw_original = raw_text
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        # Handle "```json" prefix
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        llm_result = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning(f"[rfq-creation] #{email_tracking_id}: LLM returned invalid JSON: {raw_text[:300]}")
        return [], {"error": "LLM returned invalid JSON", "raw_response": raw_original[:1000]}

    # Store the raw response for debugging
    llm_result["_raw_response"] = raw_original[:2000]

    # Items are already in standard 5-field format from the LLM prompt
    # ({input_description, input_code, brand, quantity, uom, confidence}).
    # No _normalize_to_standard_columns() needed — that's for CSV/table import.
    items = []
    if llm_result.get("has_items") and llm_result.get("items"):
        items = [item for item in llm_result["items"] if item.get("input_description")]

    # Deduplicate
    items = _deduplicate_items(items)

    logger.info(f"[rfq-creation] #{email_tracking_id}: extracted {len(items)} items "
                f"(confidence: {', '.join(i.get('confidence', '?') for i in items)})")

    return items, llm_result
