"""Gmail draft service for creating and managing email drafts.

Provides functions to:
- Create draft emails with custom X-Agent-* headers
- Generate Gmail compose URLs for browser modal
- Detect when drafts are sent and extract message IDs
- Link drafts to RFQs in the database
"""

import base64
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

from includes.gmail import get_gmail_client, check_recipient_allowed, RecipientBlockedError
from includes.dashboard.database import get_session
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

_DATA_URI_IMG = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*(["'])(data:image/(?P<sub>[a-zA-Z0-9.+-]+);base64,(?P<b64>[^"']+))\1""",
    re.IGNORECASE,
)
MAX_INLINE_IMAGES = 20
_JSON_RAW_LIMIT = 4 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Strip CR/LF and path components from an attachment filename."""
    name = (name or "attachment").replace("\x00", "")
    name = re.sub(r"[\r\n]", "", name)[:200]
    return name or "attachment"


def _inline_images_to_cid(body_html: str) -> tuple[str, list[MIMEImage]]:
    """Replace data: image URIs with cid: refs and return the inline parts."""
    parts: list[MIMEImage] = []

    def _sub(m: re.Match) -> str:
        if len(parts) >= MAX_INLINE_IMAGES:
            return m.group(0)
        try:
            raw = base64.b64decode(m.group("b64"), validate=True)
        except Exception:
            return m.group(0)  # leave malformed data URIs untouched
        cid = f"img{uuid.uuid4().hex[:12]}"
        img = MIMEImage(raw, _subtype=m.group("sub").lower())
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.{m.group('sub')}")
        parts.append(img)
        return m.group(0).replace(m.group(2), f"cid:{cid}")

    return _DATA_URI_IMG.sub(_sub, body_html or ""), parts


def _html_to_text(html: str) -> str:
    import html2text  # already a dependency
    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0
    return h.handle(html or "").strip()


def _build_mime_message(
    user_email: str,
    recipient_email: str,
    subject: str,
    body_html: str,
    headers: dict,
    attachments: list[dict] | None = None,  # [{filename, mime_type, data}]
    body_plain: str | None = None,
) -> MIMEMultipart:
    """Build a properly nested MIME message:

    multipart/mixed               <- root; file attachments live here
    +- multipart/related          <- only when inline images exist
    |  +- multipart/alternative
    |  |  +- text/plain
    |  |  +- text/html            <- cid: references
    |  +- image/* (Content-ID, inline)
    +- application/pdf ...        <- Content-Disposition: attachment
    """
    html, inline_parts = _inline_images_to_cid(body_html)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(body_plain if body_plain is not None else _html_to_text(html), "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))

    if inline_parts:
        related = MIMEMultipart("related")
        related.attach(alternative)
        for p in inline_parts:
            related.attach(p)
        content_root = related
    else:
        content_root = alternative

    root = MIMEMultipart("mixed")
    root.attach(content_root)

    for att in attachments or []:
        maintype, _, subtype = (att.get("mime_type") or "application/octet-stream").partition("/")
        part = MIMEBase(maintype, subtype or "octet-stream")
        part.set_payload(att["data"])
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=_safe_filename(att.get("filename") or ""))
        root.attach(part)

    root["to"] = recipient_email
    root["from"] = user_email
    root["subject"] = re.sub(r"[\r\n]", " ", subject)  # header injection guard
    for k, v in headers.items():
        if v:
            root[k] = re.sub(r"[\r\n]", " ", str(v))
    return root


def _gmail_send(service, msg) -> dict:
    """Send via Gmail API, using the media upload path for large messages."""
    raw = msg.as_bytes()
    if len(raw) < _JSON_RAW_LIMIT:
        return service.users().messages().send(
            userId="me", body={"raw": base64.urlsafe_b64encode(raw).decode()}
        ).execute()
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="message/rfc822", resumable=True)
    return service.users().messages().send(userId="me", body={}, media_body=media).execute()


def _gmail_create_draft(service, msg) -> dict:
    """Create a draft via Gmail API, using the media upload path for large messages."""
    raw = msg.as_bytes()
    if len(raw) < _JSON_RAW_LIMIT:
        return service.users().drafts().create(
            userId="me", body={"message": {"raw": base64.urlsafe_b64encode(raw).decode()}}
        ).execute()
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="message/rfc822", resumable=True)
    return service.users().drafts().create(
        userId="me", body={"message": {}}, media_body=media
    ).execute()


def create_draft_email(
    user_email: str,
    recipient_email: str,
    subject: str,
    body_html: str,
    rfq_id: str,
    email_type: str = "rfq_outreach",
    opportunity_id: str | None = None,
    body_plain: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Create a draft email with custom tracking headers.
    
    Args:
        user_email: Staff member email (impersonation target)
        recipient_email: Recipient email address
        subject: Email subject (will include [RFQ-<id>] token)
        body_html: HTML email body
        rfq_id: RFQ ID for tracking
        email_type: Type of email (e.g., 'rfq_outreach', 'quote', 'invoice')
        opportunity_id: Optional secondary tracking ID (e.g., HubSpot Deal ID)
        body_plain: Plain text version (optional, falls back to html2text)
        attachments: Optional list of {filename, mime_type, data} dicts
        
    Returns:
        dict with:
            - status: "ok" | "error"
            - draft_id: Gmail draft ID (on success)
            - thread_id: Gmail thread ID (on success)
            - compose_url: Browser compose URL
            - message: Success or error message
            - details: Full response (on error)
    """
    try:
        check_recipient_allowed(recipient_email)
        service = get_gmail_client(user_email)
        
        # Custom tracking headers (immutable — survive subject/body edits)
        headers = {
            "X-Eagle-OP": rfq_id,
            "X-Eagle-Type": email_type,
            "X-Eagle-RFQ": rfq_id,
        }
        if opportunity_id:
            headers["X-Eagle-Opportunity"] = opportunity_id

        msg = _build_mime_message(
            user_email=user_email,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            headers=headers,
            attachments=attachments,
            body_plain=body_plain,
        )
        
        # Create draft via Gmail API
        draft_result = _gmail_create_draft(service, msg)
        
        draft_id = draft_result.get("id")
        message_id = draft_result.get("message", {}).get("id")
        thread_id = draft_result.get("message", {}).get("threadId")
        
        # Use the draft route URL for reliable opening of the exact draft.
        # This opens full Gmail UI (accepted UX) but avoids blank compose edge cases.
        compose_url = generate_compose_url(message_id, user_email)
        
        # Log to database
        _save_draft_to_tracking(
            gmail_thread_id=thread_id,
            gmail_draft_id=draft_id,
            user_email=user_email,
            rfq_id=rfq_id,
            opportunity_id=opportunity_id,
            email_type=email_type,
            subject=subject,
            recipient_email=recipient_email,
            compose_url=compose_url,
        )
        
        logger.info(
            f"[draft-create] Created draft {draft_id} for {rfq_id} "
            f"to {recipient_email} from {user_email}"
        )
        
        return {
            "status": "ok",
            "draft_id": draft_id,
            "thread_id": thread_id,
            "compose_url": compose_url,
            "message": f"Draft created successfully. Compose URL ready.",
        }
        
    except HttpError as e:
        error_details = e.content.decode() if e.content else str(e)
        logger.error(f"[draft-create] Gmail API error: {error_details}")
        return {
            "status": "error",
            "message": f"Failed to create draft: {str(e)}",
            "details": error_details,
        }
    except Exception as e:
        logger.error(f"[draft-create] Unexpected error: {e}")
        return {
            "status": "error",
            "message": f"Unexpected error creating draft: {str(e)}",
            "details": str(e),
        }


def send_email_direct(
    user_email: str,
    recipient_email: str,
    subject: str,
    body_html: str,
    rfq_id: str,
    email_type: str = "rfq_outreach",
    opportunity_id: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Send an HTML email directly via Gmail API and track it as sent."""
    try:
        check_recipient_allowed(recipient_email)
        service = get_gmail_client(user_email)

        headers = {
            "X-Eagle-OP": rfq_id,
            "X-Eagle-Type": email_type,
            "X-Eagle-RFQ": rfq_id,
        }
        if opportunity_id:
            headers["X-Eagle-Opportunity"] = opportunity_id

        msg = _build_mime_message(
            user_email=user_email,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            headers=headers,
            attachments=attachments,
        )

        send_result = _gmail_send(service, msg)

        message_id = send_result.get("id")
        thread_id = send_result.get("threadId")

        _save_sent_to_tracking(
            gmail_thread_id=thread_id,
            gmail_message_id=message_id,
            user_email=user_email,
            rfq_id=rfq_id,
            opportunity_id=opportunity_id,
            email_type=email_type,
            subject=subject,
            recipient_email=recipient_email,
        )

        return {
            "status": "ok",
            "message_id": message_id,
            "thread_id": thread_id,
            "message": "Email sent successfully",
        }
    except HttpError as e:
        error_details = e.content.decode() if e.content else str(e)
        logger.error(f"[direct-send] Gmail API error: {error_details}")
        return {"status": "error", "message": f"Failed to send email: {str(e)}", "details": error_details}
    except Exception as e:
        logger.error(f"[direct-send] Unexpected error: {e}")
        return {"status": "error", "message": f"Unexpected error sending email: {str(e)}", "details": str(e)}


def generate_compose_url(message_id: str, user_email: str) -> str:
    """Generate a reliable Gmail URL that opens the exact draft.

    Uses #drafts/<message_id> route which consistently opens the saved draft
    (in full Gmail UI) for user editing and sending.

    Args:
        message_id: Message ID from draft_result["message"]["id"]
        user_email: Staff member email (for authuser parameter)

    Returns:
        URL that opens the specific draft in Gmail
    """
    encoded_email = quote(user_email)
    return f"https://mail.google.com/mail/u/?authuser={encoded_email}#drafts/{message_id}"


def _save_draft_to_tracking(
    gmail_thread_id: str,
    gmail_draft_id: str,
    user_email: str,
    rfq_id: str,
    email_type: str,
    subject: str,
    recipient_email: str,
    compose_url: str,
    opportunity_id: str | None = None,
) -> None:
    """Save draft info to email_tracking table.
    
    Args:
        gmail_thread_id: Gmail thread ID
        gmail_draft_id: Gmail draft ID
        user_email: Staff member email
        rfq_id: RFQ ID
        email_type: Type of email
        subject: Email subject
        recipient_email: Recipient email
        compose_url: Compose URL
        opportunity_id: Optional secondary tracking ID
    """
    try:
        from sqlalchemy import case, text, func
        from includes.dashboard.models import Contact
        
        session = get_session()
        try:
            # Look up supplier from recipient email
            supplier_id = None
            if recipient_email:
                contact = session.query(Contact).filter(
                    func.lower(Contact.email) == recipient_email.lower().strip(),
                    Contact.isinactive == False,
                ).order_by(
                    case(
                        (Contact.supplier_id.isnot(None), 0),
                        (Contact.customer_id.isnot(None), 1),
                        else_=2,
                    )
                ).first()
                if contact and contact.supplier_id:
                    supplier_id = contact.supplier_id

            session.execute(
                text("""
                    INSERT INTO email_tracking (
                        gmail_thread_id,
                        gmail_draft_id,
                        user_email,
                        sender_email,
                        rfq_id,
                        opportunity_id,
                        supplier_id,
                        direction,
                        email_type,
                        subject,
                        recipient_email,
                        draft_url,
                        created_at
                    ) VALUES (
                        :thread_id,
                        :draft_id,
                        :user_email,
                        :sender_email,
                        :rfq_id,
                        :opportunity_id,
                        :supplier_id,
                        :direction,
                        :email_type,
                        :subject,
                        :recipient_email,
                        :compose_url,
                        NOW()
                    )
                """),
                {
                    "thread_id": gmail_thread_id,
                    "draft_id": gmail_draft_id,
                    "user_email": user_email,
                    "sender_email": user_email,
                    "rfq_id": rfq_id,
                    "opportunity_id": opportunity_id,
                    "supplier_id": str(supplier_id) if supplier_id else None,
                    "direction": "draft",
                    "email_type": email_type,
                    "subject": subject,
                    "recipient_email": recipient_email,
                    "compose_url": compose_url,
                }
            )
            session.commit()
            logger.info(f"[draft-tracking] Saved draft {gmail_draft_id} to email_tracking")
        except Exception as e:
            session.rollback()
            logger.error(f"[draft-tracking] Failed to save draft to DB: {e}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"[draft-tracking] Error in _save_draft_to_tracking: {e}")


def _save_sent_to_tracking(
    gmail_thread_id: str,
    gmail_message_id: str,
    user_email: str,
    rfq_id: str,
    email_type: str,
    subject: str,
    recipient_email: str,
    opportunity_id: str | None = None,
) -> None:
    """Save sent email info to email_tracking table."""
    try:
        from sqlalchemy import case, text, func
        from includes.dashboard.models import Contact, Supplier

        session = get_session()
        try:
            # Look up supplier from recipient email
            supplier_id = None
            if recipient_email:
                contact = session.query(Contact).filter(
                    func.lower(Contact.email) == recipient_email.lower().strip(),
                    Contact.isinactive == False,
                ).order_by(
                    case(
                        (Contact.supplier_id.isnot(None), 0),
                        (Contact.customer_id.isnot(None), 1),
                        else_=2,
                    )
                ).first()
                if contact and contact.supplier_id:
                    supplier_id = contact.supplier_id

            session.execute(
                text("""
                    INSERT INTO email_tracking (
                        gmail_thread_id,
                        gmail_message_id,
                        user_email,
                        rfq_id,
                        opportunity_id,
                        supplier_id,
                        direction,
                        email_type,
                        subject,
                        recipient_email,
                        sent_at,
                        created_at
                    ) VALUES (
                        :thread_id,
                        :message_id,
                        :user_email,
                        :rfq_id,
                        :opportunity_id,
                        :supplier_id,
                        :direction,
                        :email_type,
                        :subject,
                        :recipient_email,
                        NOW(),
                        NOW()
                    )
                """),
                {
                    "thread_id": gmail_thread_id,
                    "message_id": gmail_message_id,
                    "user_email": user_email,
                    "rfq_id": rfq_id,
                    "opportunity_id": opportunity_id,
                    "supplier_id": str(supplier_id) if supplier_id else None,
                    "direction": "sent",
                    "email_type": email_type,
                    "subject": subject,
                    "recipient_email": recipient_email,
                }
            )
            session.commit()
            logger.info(f"[sent-tracking] Saved sent message {gmail_message_id} to email_tracking")
        except Exception as e:
            session.rollback()
            logger.error(f"[sent-tracking] Failed to save sent message to DB: {e}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"[sent-tracking] Error in _save_sent_to_tracking: {e}")


def get_draft_info(draft_id: str, user_email: str) -> dict | None:
    """Fetch draft info from Gmail API.
    
    Args:
        draft_id: Gmail draft ID
        user_email: Staff member email
        
    Returns:
        dict with draft details, or None if not found
    """
    try:
        service = get_gmail_client(user_email)
        draft = service.users().drafts().get(userId="me", id=draft_id).execute()
        
        return {
            "id": draft.get("id"),
            "thread_id": draft.get("message", {}).get("threadId"),
            "headers": draft.get("message", {}).get("payload", {}).get("headers", []),
        }
    except HttpError as e:
        if e.resp.status == 404:
            logger.debug(f"Draft {draft_id} not found")
            return None
        logger.error(f"Error fetching draft {draft_id}: {e}")
        return None


def delete_draft(draft_id: str, user_email: str) -> bool:
    """Delete a draft email.
    
    Args:
        draft_id: Gmail draft ID
        user_email: Staff member email
        
    Returns:
        True if successful, False otherwise
    """
    try:
        service = get_gmail_client(user_email)
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        logger.info(f"[draft-delete] Deleted draft {draft_id}")
        return True
    except HttpError as e:
        logger.error(f"[draft-delete] Failed to delete draft {draft_id}: {e}")
        return False


def extract_headers_from_message(message: dict) -> dict:
    """Extract custom tracking headers from a Gmail message.
    
    Args:
        message: Gmail message dict from API
        
    Returns:
        dict with extracted headers:
            - x_agent_op: RFQ ID
            - x_agent_type: Email type
            - x_agent_rfq: RFQ ID (should match x_agent_op)
            - x_agent_opportunity: Secondary tracking ID
            - subject: Email subject
            - from_email: From email
            - to_email: To email
    """
    headers = {}
    payload_headers = message.get("payload", {}).get("headers", [])
    
    header_map = {
        "x-agent-op": "x_agent_op",
        "x-agent-type": "x_agent_type",
        "x-agent-rfq": "x_agent_rfq",
        "x-agent-opportunity": "x_agent_opportunity",
        "subject": "subject",
        "from": "from_email",
        "to": "to_email",
    }
    
    for header_obj in payload_headers:
        name = header_obj.get("name", "").lower()
        value = header_obj.get("value", "")
        
        if name in header_map:
            headers[header_map[name]] = value
    
    return headers
