"""Gmail draft service for creating and managing email drafts.

Provides functions to:
- Create draft emails with custom X-Agent-* headers
- Generate Gmail compose URLs for browser modal
- Detect when drafts are sent and extract message IDs
- Link drafts to RFQs in the database
"""

import base64
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import quote

from includes.gmail import get_gmail_client
from includes.dashboard.database import get_session
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def create_draft_email(
    user_email: str,
    recipient_email: str,
    subject: str,
    body_html: str,
    rfq_id: str,
    email_type: str = "rfq_outreach",
    opportunity_id: str | None = None,
    body_plain: str | None = None,
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
        body_plain: Plain text version (optional, falls back to stripping HTML)
        
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
        service = get_gmail_client(user_email)
        
        # Add RFQ token to subject if not already present
        if not f"[RFQ-{rfq_id}]" in subject:
            subject = f"[RFQ-{rfq_id}] {subject}"
        
        # Create MIME message with custom headers
        msg = MIMEText(body_html, "html")
        msg["to"] = recipient_email
        msg["from"] = user_email
        msg["subject"] = subject
        
        # Add custom tracking headers
        msg["X-Agent-OP"] = rfq_id
        msg["X-Agent-Type"] = email_type
        msg["X-Agent-RFQ"] = rfq_id
        if opportunity_id:
            msg["X-Agent-Opportunity"] = opportunity_id
        
        # Encode message
        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        
        # Create draft via Gmail API
        draft_body = {"message": {"raw": raw_message}}
        draft_result = service.users().drafts().create(userId="me", body=draft_body).execute()
        
        draft_id = draft_result.get("id")
        thread_id = draft_result.get("message", {}).get("threadId")
        
        # Generate compose URL
        compose_url = generate_compose_url(draft_id, user_email)
        
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


def generate_compose_url(draft_id: str, user_email: str) -> str:
    """Generate Gmail compose URL for browser modal.
    
    Opens Gmail compose window with the given draft pre-loaded.
    
    Args:
        draft_id: Gmail draft ID
        user_email: Staff member email (for authuser parameter)
        
    Returns:
        URL suitable for browser modal: https://mail.google.com/mail/u/?...
    """
    # URL format: https://mail.google.com/mail/u/?authuser=<email>&view=cm&fs=1&compose=<draftId>
    # fs=1 means "fullscreen" mode (opens in modal/new window)
    # compose=<id> preloads the draft
    
    encoded_email = quote(user_email)
    return f"https://mail.google.com/mail/u/?authuser={encoded_email}&view=cm&fs=1&compose={draft_id}"


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
        from sqlalchemy import text
        
        session = get_session()
        try:
            session.execute(
                text("""
                    INSERT INTO email_tracking (
                        gmail_thread_id,
                        gmail_draft_id,
                        user_email,
                        rfq_id,
                        opportunity_id,
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
                        :rfq_id,
                        :opportunity_id,
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
                    "rfq_id": rfq_id,
                    "opportunity_id": opportunity_id,
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
