"""Shared email pipeline infrastructure.

Reusable components for email analysis pipelines:
- LLM call with retry and model fallback
- Image signature detection and caching
- Attachment content extraction (PDF, image, spreadsheet)

Pipeline-specific logic (prompts, interpretation, DB writes) stays in
each pipeline's own module (e.g. supplier_quote_pipeline.py).
"""

import base64
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import logging
import os
import re
import time

from config.settings import Config

logger = logging.getLogger(__name__)

# Model to try when the primary fails. This was hardcoded to
# "gemini-2.0-flash", which now returns 404 NOT_FOUND — so the "fallback" was a
# dead end that turned a recoverable 429 into a hard failure. Sourced from
# config so it changes without a code edit.
FALLBACK_MODEL = Config.FALLBACK_MODEL

# Error classes worth trying another model for. Anything else (bad request,
# auth, not-found) is a permanent condition that retrying cannot fix.
_RETRYABLE_ERRORS = frozenset(
    {"rate_limit", "unavailable", "deadline", "server_error", "timeout"}
)

_RETRY_HINT_RE = re.compile(r"retry in ([0-9.]+)\s*(s|seconds)?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def get_pipeline_model(pipeline: str, step: str) -> str:
    """Resolve the LLM model for a pipeline step.

    Resolution order:
      1. Step-specific env var: {PIPELINE}_{STEP}_MODEL (e.g. QUOTE_CLASSIFY_MODEL)
      2. Pipeline-level env var: {PIPELINE}_PIPELINE_MODEL (e.g. QUOTE_PIPELINE_MODEL)
      3. Config.SYNC_MODEL when running inside a background sync workload,
         otherwise Config.DEFAULT_MODEL

    Step 3 is the fix for our own background work contending with live chat:
    a pipeline with no explicit model would inherit DEFAULT_MODEL, which is the
    same model the chat agent uses.

    Note that an *explicit* per-pipeline model always wins, sync or not. Those
    are deliberate quality choices (RFQ_CREATION_EXTRACT_MODEL is a Pro model)
    and overriding them for being background work would silently downgrade a
    user-visible output.

    Args:
        pipeline: Pipeline name prefix, e.g. "QUOTE", "CUSTOMER_REQUEST"
        step: Step name, e.g. "classify", "extract", "interpret"
    """
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

    from includes.llm.context import is_sync

    if is_sync() and Config.SYNC_MODEL:
        return Config.SYNC_MODEL

    return Config.DEFAULT_MODEL


# ---------------------------------------------------------------------------
# LLM call with retry + model fallback
# ---------------------------------------------------------------------------

def get_pipeline_candidates(pipeline: str, step: str) -> list[str]:
    """Ordered, de-duplicated models to try for a pipeline step.

    The old code tried ``[primary, primary, FALLBACK_MODEL]`` — i.e. it burned
    both its retries on the *same* model that was already overloaded, then fell
    back to one that 404s. Trying distinct models is the whole point of having a
    fallback.

    Candidates are the primary first, then the configured ladder with anything
    equal to the primary removed. That removal matters: with our current .env
    the single FALLBACK_MODEL *is* the primary for the QUOTE pipeline, so a
    naive implementation would produce a one-element list and quietly have no
    failover.
    """
    primary = get_pipeline_model(pipeline, step)
    ladder = list(Config.FALLBACK_CHAIN)
    if FALLBACK_MODEL and FALLBACK_MODEL not in ladder:
        ladder.insert(0, FALLBACK_MODEL)

    candidates = [primary]
    candidates.extend(m for m in ladder if m and m != primary)
    return candidates


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a server-suggested delay, if the API sent one.

    Checks a Retry-After header first, then falls back to the SDK's
    "... retry in 1.42 seconds" message. Returns None when nothing is suggested,
    letting the caller use plain exponential backoff.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    match = _RETRY_HINT_RE.search(str(exc))
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            pass
    return None


def _http_options(timeout_ms: int, service_tier: str | None):
    """Build HttpOptions with a bounded retry policy and optional service tier."""
    from google.genai import types as _types

    options = _types.HttpOptions(
        timeout=timeout_ms,
        retry_options=_types.HttpRetryOptions(
            attempts=Config.LLM_SDK_RETRY_ATTEMPTS,
            initial_delay=1.0,
            exp_base=2.0,
            max_delay=8.0,
            jitter=1.0,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    )
    if service_tier:
        # Vertex wants the proto enum name (SERVICE_TIER_PRIORITY), not
        # "priority". The SDK has no first-class field for this in 1.68.0.
        options.extra_body = {"service_tier": service_tier}
    return options


def llm_call_with_retry(
    pipeline: str,
    step: str,
    contents,
    temperature: float = 0.1,
    timeout: int | None = None,
    scope: str | None = None,
    correlation_id: str | None = None,
):
    """Call Gemini, walking distinct models on transient failure.

    Strategy:
      1. Try the pipeline/step's primary model.
      2. On a *retryable* error, wait (honouring ``Retry-After`` when the API
         sends one) and try the next distinct candidate.
      3. Give up at ``Config.LLM_MAX_ATTEMPT_SECONDS`` or when candidates run
         out, whichever comes first.

    Permanent errors (400/401/403/404) raise immediately — retrying them just
    wastes the user's time.

    Every attempt is recorded to llm_call_log with its own row, so a failover
    is visible after the fact rather than only when it fails outright.

    Args:
        pipeline: Pipeline name prefix (e.g. "QUOTE")
        step: Step name (e.g. "classify", "extract", "interpret")
        contents: Gemini contents (string, list of Parts, etc.)
        temperature: LLM temperature
        timeout: HTTP timeout in milliseconds (defaults to config)
        scope: Telemetry scope; defaults to ``pipeline:{pipeline}/{step}``
        correlation_id: thread_id / rfq_number, to stitch a turn together

    Returns the response object. Raises on permanent failure or exhaustion.
    """
    from google import genai as _genai
    from google.genai import types as _types

    from includes.llm.telemetry import classify_error, instrument_call

    from includes.llm.context import SYNC, current_service_tier, current_workload

    timeout = timeout or Config.LLM_REQUEST_TIMEOUT_MS
    service_tier = current_service_tier()
    if scope is None:
        # Prefix background work so telemetry can separate sync traffic from
        # user-triggered traffic on the same pipeline.
        prefix = "sync" if current_workload() == SYNC else "pipeline"
        scope = f"{prefix}:{pipeline}/{step}"
    candidates = get_pipeline_candidates(pipeline, step)
    deadline = time.monotonic() + Config.LLM_MAX_ATTEMPT_SECONDS

    last_error: BaseException | None = None
    previous_model: str | None = None

    for index, model in enumerate(candidates):
        if time.monotonic() >= deadline:
            logger.warning(
                f"[email-pipeline] {pipeline}/{step}: giving up after "
                f"{Config.LLM_MAX_ATTEMPT_SECONDS}s budget"
            )
            break
        try:
            with instrument_call(
                scope=scope,
                model=model,
                location=Config.GOOGLE_CLOUD_LOCATION,
                service_tier=service_tier,
                attempt=index + 1,
                fell_back_from=previous_model,
                correlation_id=correlation_id,
            ) as record:
                client = _genai.Client(
                    http_options=_http_options(timeout, service_tier)
                )
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=_types.GenerateContentConfig(temperature=temperature),
                )
                record.set_response(response)
            if index > 0:
                logger.info(
                    f"[email-pipeline] {pipeline}/{step}: succeeded after "
                    f"falling back to {model} (from {previous_model})"
                )
            return response
        except Exception as e:  # noqa: BLE001 - re-raised below unless retryable
            last_error = e
            error_class, _status = classify_error(e)
            if error_class not in _RETRYABLE_ERRORS:
                raise  # permanent — do not retry
            previous_model = model
            logger.warning(
                f"[email-pipeline] {pipeline}/{step}: attempt {index + 1} "
                f"failed (model={model}, error={error_class}): {e}"
            )
            if index < len(candidates) - 1:
                delay = _retry_after_seconds(e)
                if delay is None:
                    delay = 2.0 ** index
                remaining = deadline - time.monotonic()
                delay = min(delay, max(0.0, remaining))
                if delay > 0:
                    time.sleep(delay)

    raise last_error if last_error else RuntimeError(
        f"{pipeline}/{step}: no model candidates available"
    )


# ---------------------------------------------------------------------------
# Database session helper
# ---------------------------------------------------------------------------

def _get_session():
    """Get a synchronous SQLAlchemy session."""
    from includes.dashboard.database import get_session
    return get_session()


# ---------------------------------------------------------------------------
# Image signature detection & caching
# ---------------------------------------------------------------------------

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
            logger.debug(f"[email-pipeline] Skipping known signature: {filename}")
        return cached

    # 2. Classify unknown image
    classification = classify_image_content(image_bytes, mime_type, pipeline)

    # 3. Cache for future
    store_image_signature(image_bytes, classification, filename, source_email_id)
    if classification == "signature":
        logger.debug(f"[email-pipeline] New signature detected & cached: {filename}")
    return classification


# ---------------------------------------------------------------------------
# Gmail attachment fetch
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Attachment content extraction
#
# Failures are reported as CODES, not marker strings. Previously the failure
# WAS the content — extract_pdf_content returned "*[PDF extraction failed:
# ...]*", which got appended to the bundle and fed to the extraction LLM. So a
# caller could not tell a failed attachment from one whose text legitimately
# contained those words, and "9 of 10 attachments read" was not representable.
#
# A closed set of codes means callers can branch on them (the same reason HTTP
# has status classes); `detail` carries the upstream message for humans only.
# ---------------------------------------------------------------------------

# What the LLM sees in place of content we never got. Deliberately neutral, so
# upstream error text stops leaking into the prompt.
PLACEHOLDER_UNREADABLE = "*[Attachment could not be read]*"
PLACEHOLDER_EMPTY = "*[No content extracted]*"
PLACEHOLDER_UNPARSED = "*[Spreadsheet could not be parsed]*"

# Cap upstream error text so stored JSONB stays readable.
_DETAIL_MAX = 200


def _short(exc: object) -> str:
    """One-line, length-capped failure detail. Never used for branching."""
    return " ".join(str(exc).split())[:_DETAIL_MAX]


class AttachmentFailure(str, Enum):
    """Why one attachment's content is missing from the bundle.

    UNSUPPORTED is a deterministic gap (retrying will not help) and is kept
    distinct from MODEL_ERROR (transient) so confidence scoring can later weight
    them differently.
    """
    FETCH_FAILED = "fetch_failed"   # bytes could not be downloaded from Gmail
    UNSUPPORTED = "unsupported"     # mime type we do not handle at all
    MODEL_ERROR = "model_error"     # LLM raised after retries (5xx, timeout, ...)
    EMPTY = "empty"                 # LLM returned nothing, or the document was blank
    PARSE_ERROR = "parse_error"     # local parsing failed (xlsx / csv)


class BundleFailure(str, Enum):
    """Why the whole bundle is unusable."""
    EMAIL_NOT_FOUND = "email_not_found"
    NO_CONTENT = "no_content"       # no body AND no readable attachments


@dataclass
class AttachmentExtraction:
    """One attachment's contribution to the bundle, plus how it went."""
    text: str
    failure: AttachmentFailure | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass
class ContentBundle:
    """Everything an email gave us to work with, plus what it did not.

    ``text`` is the Markdown bundle handed to extraction. The counters and
    ``failures`` are metadata that did not previously exist as data, which made
    "the pipeline read 9 of 10 attachments" unrepresentable — an attachment could
    vanish and the result still looked complete.

    ``failures`` entries are ``{filename, code, detail}`` with ``code`` from
    AttachmentFailure, shaped so they can be stored in JSONB as-is.
    """
    text: str = ""
    attachment_total: int = 0
    attachment_read: int = 0
    skipped_as_signature: int = 0
    failures: list[dict] = field(default_factory=list)
    bundle_failure: BundleFailure | None = None

    @property
    def complete(self) -> bool:
        """True when the bundle is usable and every attachment contributed."""
        return self.bundle_failure is None and not self.failures

    def to_dict(self) -> dict:
        """Storage shape for the pipeline result's ``input`` key."""
        return {
            "attachment_total": self.attachment_total,
            "attachment_read": self.attachment_read,
            "skipped_as_signature": self.skipped_as_signature,
            "failures": list(self.failures),
            "bundle_failure": self.bundle_failure.value if self.bundle_failure else None,
        }


def extract_pdf_content(
    pdf_bytes: bytes,
    filename: str,
    pipeline: str = "QUOTE",
) -> AttachmentExtraction:
    """Extract text and tabular data from a PDF via Gemini.

    Never raises: a failure comes back as an AttachmentExtraction carrying a
    code, so the caller can record what could not be read.
    """
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
            return AttachmentExtraction(
                PLACEHOLDER_EMPTY, AttachmentFailure.EMPTY, "model returned no text"
            )
        return AttachmentExtraction(response.text)
    except Exception as e:
        logger.error(f"Gemini PDF extraction failed for {filename}: {e}")
        return AttachmentExtraction(
            PLACEHOLDER_UNREADABLE, AttachmentFailure.MODEL_ERROR, _short(e)
        )


def extract_image_content(
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    pipeline: str = "QUOTE",
) -> AttachmentExtraction:
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
        if not response.text:
            return AttachmentExtraction(
                PLACEHOLDER_EMPTY, AttachmentFailure.EMPTY, "model returned no text"
            )
        return AttachmentExtraction(response.text)
    except Exception as e:
        logger.error(f"Gemini image extraction failed for {filename}: {e}")
        return AttachmentExtraction(
            PLACEHOLDER_UNREADABLE, AttachmentFailure.MODEL_ERROR, _short(e)
        )


def extract_spreadsheet_content(data: bytes, filename: str, mime_type: str) -> AttachmentExtraction:
    """Extract spreadsheet content. CSV parsed directly; Excel via openpyxl.

    No LLM call — purely local parsing.
    """
    if filename.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8", errors="replace")
            return AttachmentExtraction(f"```csv\n{text[:5000]}\n```")
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
            return AttachmentExtraction(
                PLACEHOLDER_EMPTY, AttachmentFailure.EMPTY, "spreadsheet has no rows"
            )
        return AttachmentExtraction(result_text[:8000])
    except Exception as e:
        logger.error(f"Local spreadsheet extraction failed for {filename}: {e}")
        return AttachmentExtraction(
            PLACEHOLDER_UNPARSED, AttachmentFailure.PARSE_ERROR, _short(e)
        )
