"""AI summary of RFQ communications — powers the [AI Summary] button on the comms tab.

Flow:
  1. Compute a cache key from the RFQ's email-tracking fingerprint (count,
     max id, latest timestamps) and a fingerprint of the quoted-suppliers state.
  2. If the key matches what's stored on the RFQ, return the cached markdown.
  3. Otherwise build a compact raw bundle and ask the LLM to summarise it,
     then persist {cache_key, generated_at, markdown} on the RFQ row.

LLM calls go through the shared includes/email_pipeline helper (retry +
model fallback), and respect an optional COMMS_SUMMARY_MODEL env var.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from includes.email_pipeline import llm_call_with_retry

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT: Optional[str] = None


def _get_session():
    """Get a synchronous SQLAlchemy session."""
    from includes.dashboard.database import get_session
    return get_session()


def _load_prompt() -> str:
    """Load the summary prompt from config (cached in-process)."""
    global _SUMMARY_PROMPT
    if _SUMMARY_PROMPT is not None:
        return _SUMMARY_PROMPT
    prompt_path = Path(__file__).parent.parent.parent / "config" / "prompts" / "comms_summary.md"
    try:
        _SUMMARY_PROMPT = prompt_path.read_text()
    except OSError:
        logger.warning(f"[comms-summary] prompt file not found: {prompt_path}")
        _SUMMARY_PROMPT = (
            "Summarise the RFQ communications under four markdown headings: "
            "## Dates, ## Quotes received, ## Clarification required, ## No response."
        )
    return _SUMMARY_PROMPT


# ---------------------------------------------------------------------------
# Date decoration (client-side relative time) — applied per response, never
# stored, so the cache keeps the raw markdown untouched.
# ---------------------------------------------------------------------------

_DATE_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?\b")


def decorate_dates(markdown: str) -> str:
    """Wrap YYYY-MM-DD[ HH:MM] timestamps in <span data-ts=epoch> tags.

    Timestamps are assumed to be in the app timezone (TIMEZONE setting).
    The client appends a human 'time ago' label from the epoch.
    """
    from zoneinfo import ZoneInfo

    from config import config

    local_tz = ZoneInfo(config.TIMEZONE)

    def _sub(m: re.Match) -> str:
        date_s, time_s = m.group(1), m.group(2)
        try:
            if time_s:
                dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
            else:
                dt = datetime.strptime(date_s, "%Y-%m-%d")
            epoch = int(dt.replace(tzinfo=local_tz).timestamp())
        except ValueError:
            return m.group(0)  # malformed date — leave untouched
        return f'<span class="ts" data-ts="{epoch}">{m.group(0)}</span>'

    return _DATE_TS_RE.sub(_sub, markdown or "")


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def compute_cache_key(email_fingerprint: dict, quotes_fingerprint: str) -> str:
    """Deterministic cache key from email + quotes fingerprints."""
    payload = json.dumps(
        {"emails": email_fingerprint, "quotes": quotes_fingerprint},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _email_fingerprint(session, rfq_id: str, rfq_number: str) -> dict:
    """Aggregate fingerprint of email_tracking rows for this RFQ.

    Any new email, reply, or pipeline re-run changes one of these values,
    which invalidates the summary cache.
    """
    from sqlalchemy import text

    row = session.execute(
        text(
            """
            SELECT COUNT(*) AS cnt,
                   COALESCE(MAX(id), 0) AS max_id,
                   MAX(COALESCE(sent_at, created_at)) AS max_ts,
                   MAX(COALESCE(updated_at, created_at)) AS max_updated
            FROM email_tracking
            WHERE rfq_id = :rfq_id OR rfq_id = :rfq_number OR rfq_token = :rfq_number
            """
        ),
        {"rfq_id": rfq_id, "rfq_number": rfq_number or ""},
    ).mappings().one()
    return {
        "count": int(row["cnt"] or 0),
        "max_id": int(row["max_id"] or 0),
        "max_ts": row["max_ts"],
        "max_updated": row["max_updated"],
    }


def _quotes_fingerprint(rfq) -> str:
    """Hash of the quoted-suppliers state across all items."""
    quotes = [
        {"line": item.line, "suppliers": item.suppliers or []}
        for item in (rfq.items or [])
    ]
    return hashlib.sha256(
        json.dumps(quotes, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Bundle building
# ---------------------------------------------------------------------------

def _fmt_ts(ts, local_tz) -> str:
    if not ts:
        return "?"
    if isinstance(ts, str):
        return ts[:16].replace("T", " ")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(local_tz).strftime("%Y-%m-%d %H:%M")


def _email_rows(session, rfq_id: str, rfq_number: str) -> list[dict]:
    from sqlalchemy import text

    rows = session.execute(
        text(
            """
            SELECT et.direction, et.email_type, et.subject,
                   et.sender_email, et.recipient_email,
                   et.sent_at, et.created_at, et.updated_at,
                   et.supplier_pipeline_result,
                   et.customer_id, et.supplier_id,
                   s.name AS supplier_name, c.companyname AS customer_name
            FROM email_tracking et
            LEFT JOIN suppliers s ON s.id = et.supplier_id
            LEFT JOIN customers c ON c.id = et.customer_id
            WHERE et.rfq_id = :rfq_id OR et.rfq_id = :rfq_number OR et.rfq_token = :rfq_number
            ORDER BY COALESCE(et.sent_at, et.created_at) ASC, et.created_at ASC
            LIMIT 300
            """
        ),
        {"rfq_id": rfq_id, "rfq_number": rfq_number or ""},
    ).mappings().all()
    return [dict(r) for r in rows]


def _build_bundle(
    rfq_dict: dict,
    rfq_extra: dict,
    email_rows: list[dict],
    local_tz,
) -> str:
    """Build the compact raw-data bundle handed to the LLM."""
    lines: list[str] = []
    lines.append(f"RFQ: {rfq_dict.get('rfq_number')}")
    lines.append(f"Customer: {rfq_dict.get('customer')}")
    lines.append(f"RFQ status: {rfq_dict.get('status')}")
    lines.append(f"RFQ created (UTC): {rfq_dict.get('created_date')}")
    lines.append(f"Email status: {rfq_extra.get('email_status') or 'unknown'}")
    if rfq_extra.get("last_email_sent_at"):
        lines.append(f"Last email sent (UTC): {rfq_extra['last_email_sent_at']}")
    if rfq_dict.get("notes"):
        lines.append(f"RFQ notes: {(rfq_dict.get('notes') or '').strip()[:500]}")

    lines.append("")
    lines.append("## Supplier contacts")
    for s in rfq_extra.get("supplier_emails") or []:
        lines.append(f"- {s.get('name') or '?'} <{s.get('email') or '?'}>")

    lines.append("")
    lines.append("## Line items")
    for item in rfq_dict.get("items", []):
        parts = [f"line {item.get('line')}", item.get("input_description", "")]
        if item.get("input_code"):
            parts.append(f"code={item['input_code']}")
        if item.get("quantity") is not None:
            parts.append(f"qty={item['quantity']}{item.get('uom') or ''}")
        lines.append("- " + " | ".join(str(p) for p in parts).strip())

    lines.append("")
    lines.append("## Email timeline (local time, oldest first)")
    for r in email_rows:
        ts_s = _fmt_ts(r.get("sent_at") or r.get("created_at"), local_tz)
        direction = (r.get("direction") or "?").upper()
        party = (
            r.get("supplier_name")
            or r.get("customer_name")
            or r.get("sender_email")
            or r.get("recipient_email")
            or "?"
        )
        spr = r.get("supplier_pipeline_result") or {}
        classification = spr.get("classification")
        reason = spr.get("reason")
        cls_s = f" [{classification}: {reason}]" if classification else ""
        subject = (r.get("subject") or "").strip()[:120]
        lines.append(f"- {ts_s} {direction:>8} {party}: {subject}{cls_s}")

    lines.append("")
    lines.append("## Quoted suppliers on items")
    seen = set()
    for item in rfq_dict.get("items", []):
        for s in item.get("suppliers") or []:
            name = s.get("name") or "?"
            key = (name, item.get("line"), s.get("price"))
            if key in seen:
                continue
            seen.add(key)
            bits = [f"line {item.get('line')}", name]
            if s.get("price") is not None:
                bits.append(f"price={s.get('price')} {s.get('price_type') or ''}".strip())
            if s.get("lead_time"):
                bits.append(f"lead={s.get('lead_time')}")
            status = s.get("status") or s.get("quote_status")
            if status:
                bits.append(f"status={status}")
            lines.append("- " + " | ".join(str(b) for b in bits))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic "Dates" section (server-computed, not LLM-generated)
# ---------------------------------------------------------------------------

def _min_ts(rows, pred):
    vals = [r.get("sent_at") or r.get("created_at") for r in rows if pred(r)]
    return min((v for v in vals if v), default=None)


def _max_ts(rows, pred):
    vals = [r.get("sent_at") or r.get("created_at") for r in rows if pred(r)]
    return max((v for v in vals if v), default=None)


def _is_supplier_row(r: dict) -> bool:
    return bool(r.get("supplier_id") or r.get("supplier_name"))


def _is_customer_row(r: dict) -> bool:
    return bool(r.get("customer_id") or r.get("customer_name"))


def _build_dates_section(rfq, email_rows: list[dict], local_tz) -> str:
    """Compute the four key dates deterministically from the raw data."""
    lines = ["## Dates"]

    customer_req = _min_ts(
        email_rows, lambda r: r.get("direction") == "received" and _is_customer_row(r)
    )
    if customer_req is None:
        customer_req = _min_ts(email_rows, lambda r: r.get("direction") == "received")
    lines.append(
        f"- Customer's initial request was received: {_fmt_ts(customer_req, local_tz) if customer_req else '(none yet)'}"
    )

    created = getattr(rfq, "created_date", None)
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_local = created.astimezone(local_tz)
        if created_local.hour == 0 and created_local.minute == 0:
            created_s = created_local.strftime("%Y-%m-%d")
        else:
            created_s = created_local.strftime("%Y-%m-%d %H:%M")
    else:
        created_s = str(created) if created else None
    lines.append(f"- RFQ was created: {created_s or '(unknown)'}")

    first_contact = _min_ts(
        email_rows, lambda r: r.get("direction") == "sent" and _is_supplier_row(r)
    )
    if first_contact is None:
        first_contact = _min_ts(email_rows, lambda r: r.get("direction") == "sent")
    lines.append(
        f"- Suppliers were first contacted: {_fmt_ts(first_contact, local_tz) if first_contact else '(none yet)'}"
    )

    latest_response = _max_ts(
        email_rows, lambda r: r.get("direction") == "received" and _is_supplier_row(r)
    )
    lines.append(
        f"- Latest supplier response: {_fmt_ts(latest_response, local_tz) if latest_response else '(none yet)'}"
    )
    return "\n".join(lines)


_DATES_SECTION_RE = re.compile(
    r"^##\s+Dates\b[^\n]*\n.*?(?=^##\s+\w|\Z)", re.MULTILINE | re.DOTALL
)


def _strip_dates_section(markdown: str) -> str:
    """Defensively remove any '## Dates' section the LLM may have emitted."""
    return _DATES_SECTION_RE.sub("", markdown or "").strip()


# ---------------------------------------------------------------------------
# Generate / cache
# ---------------------------------------------------------------------------

def get_or_generate_summary(rfq_number: str, force: bool = False) -> dict:
    """Return the AI comms summary, generating + caching it if needed.

    Returns {"status": "ok", "markdown", "generated_at", "from_cache"}
    or {"status": "error", "message", "http_status"}.
    """
    from zoneinfo import ZoneInfo

    from config import config
    from includes.dashboard.models import RFQ
    from includes.tools.rfq_crud import _get_rfq_dict_sync

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return {"status": "error", "message": "RFQ not found", "http_status": 404}

        rfq_id = str(rfq.id)
        fingerprint = _email_fingerprint(session, rfq_id, rfq_number)
        quotes_fp = _quotes_fingerprint(rfq)
        cache_key = compute_cache_key(fingerprint, quotes_fp)

        cached = getattr(rfq, "comms_summary", None) or {}
        if (
            not force
            and cached.get("cache_key") == cache_key
            and cached.get("markdown")
        ):
            return {
                "status": "ok",
                "markdown": cached["markdown"],
                "generated_at": cached.get("generated_at"),
                "from_cache": True,
            }

        rfq_dict = _get_rfq_dict_sync(rfq_number)
        if not rfq_dict:
            return {"status": "error", "message": "RFQ not found", "http_status": 404}

        email_rows = _email_rows(session, rfq_id, rfq_number)
        local_tz = ZoneInfo(config.TIMEZONE)
        bundle = _build_bundle(
            rfq_dict,
            {
                "email_status": rfq.email_status,
                "last_email_sent_at": rfq.last_email_sent_at,
                "supplier_emails": rfq.supplier_emails,
            },
            email_rows,
            local_tz,
        )

        prompt = _load_prompt()
        logger.info(f"[comms-summary] {rfq_number}: generating (cache key {cache_key[:8]})")
        response = llm_call_with_retry(
            pipeline="COMMS",
            step="summary",
            contents=[f"{prompt}\n\n---\n\n{bundle}"],
            temperature=0.2,
            timeout=60000,
        )
        if not response.text:
            raise ValueError("LLM returned empty response")
        markdown = response.text.strip()
        if markdown.startswith("```"):
            markdown = markdown.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        # Dates are deterministic — server-computed and prepended to the LLM output
        dates_section = _build_dates_section(rfq, email_rows, local_tz)
        markdown = _strip_dates_section(markdown)
        full_markdown = f"{dates_section}\n\n{markdown}".strip()

        generated_at = datetime.now(timezone.utc).isoformat()
        rfq.comms_summary = {
            "cache_key": cache_key,
            "generated_at": generated_at,
            "markdown": full_markdown,
        }
        session.commit()
        logger.info(f"[comms-summary] {rfq_number}: saved ({len(full_markdown)} chars)")
        return {
            "status": "ok",
            "markdown": full_markdown,
            "generated_at": generated_at,
            "from_cache": False,
        }
    except Exception as e:
        session.rollback()
        logger.exception(f"[comms-summary] {rfq_number}: generation failed")
        return {"status": "error", "message": f"Summary generation failed: {e}", "http_status": 500}
    finally:
        session.close()
