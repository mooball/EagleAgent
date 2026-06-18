"""Gmail mailbox sync job — incremental scanning via History API.

Scans enabled mailboxes for new messages and runs the three-tier
matching pipeline to link emails to RFQs, suppliers, and customers.

Usage:
    uv run python -m scripts.sync_gmail_mailboxes [--init] [--user EMAIL] [--dry-run]

Options:
    --init      Initialize sync cursors for all enabled mailboxes (first run)
    --user      Scan only a specific user's mailbox
    --dry-run   Show matches without writing to database
"""

import argparse
import base64
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from config.settings import Config
from includes.dashboard.database import get_session
from includes.dashboard.models import (
    EmailTracking,
    MailboxScanConfig,
    RFQ,
)
from includes.gmail import get_gmail_client
from includes.gmail.matching import (
    build_domain_index,
    determine_direction,
    extract_domain,
    match_by_contact,
    match_by_id,
    match_by_subject,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# Only scan @eagle-exports.com mailboxes
SCAN_DOMAIN = "eagle-exports.com"


def get_enabled_mailboxes(session) -> list[str]:
    """Return list of user emails that should be scanned."""
    configs = session.query(MailboxScanConfig).filter(
        MailboxScanConfig.scan_enabled == True,
        MailboxScanConfig.user_email.like(f'%@{SCAN_DOMAIN}'),
    ).all()
    return [c.user_email for c in configs]


def initialize_cursor(session, user_email: str, dry_run: bool = False) -> int | None:
    """Seed the sync cursor with the user's latest historyId."""
    try:
        service = get_gmail_client(user_email)
        profile = service.users().getProfile(userId="me").execute()
        history_id = int(profile.get("historyId", 0))

        if dry_run:
            logger.info(f"[dry-run] Would initialize cursor for {user_email} at historyId={history_id}")
            return history_id

        session.execute(
            text("""
                INSERT INTO mailbox_sync_cursor (user_email, last_history_id, updated_at)
                VALUES (:email, :hid, NOW())
                ON CONFLICT (user_email) DO UPDATE SET last_history_id = :hid, updated_at = NOW()
            """),
            {"email": user_email, "hid": history_id},
        )
        session.commit()
        logger.info(f"Initialized cursor for {user_email} at historyId={history_id}")
        return history_id
    except Exception as e:
        logger.error(f"Failed to initialize cursor for {user_email}: {e}")
        session.rollback()
        return None


def get_cursor(session, user_email: str) -> int | None:
    """Get the last known historyId for a mailbox."""
    row = session.execute(
        text("SELECT last_history_id FROM mailbox_sync_cursor WHERE user_email = :email"),
        {"email": user_email},
    ).fetchone()
    return row[0] if row else None


def update_cursor(session, user_email: str, history_id: int):
    """Update the sync cursor after successful processing."""
    session.execute(
        text("""
            UPDATE mailbox_sync_cursor
            SET last_history_id = :hid, updated_at = NOW()
            WHERE user_email = :email
        """),
        {"email": user_email, "hid": history_id},
    )


def extract_message_metadata(service, message_id: str) -> dict | None:
    """Fetch message metadata (headers, labels) from Gmail API."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject",
                            "X-Eagle-RFQ", "X-Eagle-OP", "X-Eagle-Opportunity"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

        # Prefer Gmail's internalDate (normalized Unix timestamp) over the Date header
        sent_at = None
        internal_ts = msg.get("internalDate")
        if internal_ts:
            sent_at = datetime.fromtimestamp(int(internal_ts) / 1000, tz=timezone.utc)
        else:
            date_header = headers.get("date", "")
            if date_header:
                try:
                    sent_at = parsedate_to_datetime(date_header)
                except (ValueError, TypeError):
                    pass

        return {
            "id": msg["id"],
            "threadId": msg["threadId"],
            "historyId": msg.get("historyId"),
            "labelIds": msg.get("labelIds", []),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "date": sent_at,
            "x_eagle_rfq": headers.get("x-eagle-rfq"),
            "x_eagle_op": headers.get("x-eagle-op"),
            "x_eagle_opportunity": headers.get("x-eagle-opportunity"),
        }
    except Exception as e:
        # 404s are expected for messages deleted between history fetch and metadata fetch
        logger.debug(f"Failed to fetch message {message_id}: {e}")
        return None


def extract_email_address(header_value: str) -> str:
    """Extract bare email from a header value like 'Name <email@domain.com>'.

    Handles folded headers (embedded newlines), commas in display names,
    and other RFC 2822 quirks.
    """
    # Collapse newlines and normalize whitespace
    cleaned = ' '.join(header_value.split())
    if '<' in cleaned and '>' in cleaned:
        return cleaned.split('<')[1].split('>')[0].strip().lower()
    # No angle brackets — the whole value might be a bare email
    cleaned = cleaned.strip().lower()
    return cleaned if '@' in cleaned else ''


# Company-owned domains to exclude from external address extraction
_OWN_DOMAINS = frozenset({"eagle-exports.com", "eaglexp.com.au"})


def extract_all_addresses(msg_meta: dict) -> list[str]:
    """Extract all email addresses from From, To, Cc headers (excluding own domains)."""
    addresses = []
    for field in ("from", "to", "cc"):
        raw = msg_meta.get(field, "")
        if not raw:
            continue
        # Split multiple recipients
        for part in raw.split(','):
            addr = extract_email_address(part)
            if addr and '@' in addr:
                domain = addr.rsplit('@', 1)[1]
                if domain not in _OWN_DOMAINS:
                    addresses.append(addr)
    return addresses


def _split_html_quote(html: str) -> tuple[str, str | None]:
    """Split HTML email into new content and quoted reply.

    Detects Gmail's <div class="gmail_quote">, Outlook's forwarding blocks,
    and generic <blockquote> patterns.

    Returns: (new_html, quoted_html) — quoted_html is None if no quote found.
    """
    import re

    # Gmail: <div class="gmail_quote">
    gmail_pattern = re.search(r'<div\s+class="gmail_quote"', html, re.IGNORECASE)
    if gmail_pattern:
        return html[:gmail_pattern.start()], html[gmail_pattern.start():]

    # Outlook: <div id="divRtagSignature"> or border-top separator
    outlook_pattern = re.search(
        r'<div\s+style="[^"]*border-top:\s*solid[^"]*"',
        html, re.IGNORECASE
    )
    if outlook_pattern:
        return html[:outlook_pattern.start()], html[outlook_pattern.start():]

    # Generic: "On ... wrote:" followed by blockquote
    wrote_pattern = re.search(
        r'<div[^>]*>On\s+.{10,80}\s+wrote:\s*</div>\s*<blockquote',
        html, re.IGNORECASE
    )
    if wrote_pattern:
        return html[:wrote_pattern.start()], html[wrote_pattern.start():]

    # Forwarding headers: "From: ... Sent: ... To: ..."
    fwd_pattern = re.search(
        r'<b>From:</b>.*?<b>Sent:</b>',
        html, re.IGNORECASE | re.DOTALL
    )
    if fwd_pattern:
        # Back up to the parent div/p
        before = html[:fwd_pattern.start()]
        # Find the start of the containing element
        last_open = max(before.rfind('<div'), before.rfind('<p'))
        if last_open > 0:
            return html[:last_open], html[last_open:]
        return html[:fwd_pattern.start()], html[fwd_pattern.start():]

    return html, None


def _split_plain_quote(text: str) -> tuple[str, str | None]:
    """Split plain text email into new content and quoted reply using email-reply-parser."""
    try:
        from email_reply_parser import EmailReplyParser

        reply = EmailReplyParser.parse_reply(text)
        if reply and reply != text.strip():
            # Find where the reply ends in the original text to get the quoted part
            # email_reply_parser returns just the visible/reply portion
            quoted_start = text.find(reply) + len(reply) if reply in text else -1
            if quoted_start > 0 and quoted_start < len(text):
                quoted = text[quoted_start:].strip()
                return reply, quoted if quoted else None
            return reply, None
        return text, None
    except Exception:
        return text, None


def _clean_email_markdown(md: str) -> str:
    """Clean up email markdown for display:
    - Strip long tracking/image URLs
    - Collapse email signatures
    - Remove excessive blank lines
    """
    import re

    lines = md.split('\n')
    cleaned = []
    in_signature = False

    for line in lines:
        # Detect signature separator (--- or ___  or common sig patterns)
        if re.match(r'^[-_]{3,}\s*$', line.strip()) and len(cleaned) > 3:
            # Check if this looks like a signature separator (not a markdown HR in body)
            in_signature = True
            cleaned.append('---')
            continue

        if in_signature:
            # In signature: strip long URLs but keep the text
            # Replace [text](very-long-url) with just text
            line = re.sub(r'\[([^\]]*)\]\(https?://[^\)]{80,}\)', r'\1', line)
            # Strip standalone long URLs
            line = re.sub(r'https?://\S{80,}', '', line)
            # Strip image references
            line = re.sub(r'!\[[^\]]*\]\([^\)]*\)', '', line)
            # Skip lines that are now empty or just whitespace/pipes
            if not line.strip() or re.match(r'^[\s|]*$', line):
                continue
            cleaned.append(line)
        else:
            # In body: replace long inline URLs but keep link text
            line = re.sub(r'\[([^\]]*)\]\(https?://[^\)]{120,}\)', r'\1', line)
            # Strip tracking pixel images
            line = re.sub(r'!\[[^\]]*\]\(https?://[^\)]*\)', '', line)
            cleaned.append(line)

    # Remove excessive blank lines (3+ → 2)
    result = '\n'.join(cleaned)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def fetch_message_content(service, message_id: str) -> dict | None:
    """Fetch full message content (body + attachments manifest) from Gmail API.

    Returns dict with keys: body_html, body_markdown, attachments_json,
    sender_name, all_recipients. Returns None on failure.
    """
    try:
        import html2text

        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()

        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        # Extract sender display name
        from_header = headers.get("from", "")
        sender_name = None
        if '<' in from_header:
            sender_name = from_header.split('<')[0].strip().strip('"')
        elif from_header:
            sender_name = from_header.split('@')[0]

        # Extract all recipients
        all_recipients = []
        for rtype in ("to", "cc", "bcc"):
            raw = headers.get(rtype, "")
            if not raw:
                continue
            for part in raw.split(','):
                part = part.strip()
                if '<' in part and '>' in part:
                    name = part.split('<')[0].strip().strip('"')
                    email = part.split('<')[1].split('>')[0].strip()
                else:
                    name = ""
                    email = part.strip()
                if email:
                    all_recipients.append({"email": email.lower(), "name": name, "type": rtype})

        # Extract body (prefer HTML, fallback to plain text)
        body_html = None
        body_plain = None
        attachments = []

        def _walk_parts(parts):
            nonlocal body_html, body_plain
            for part in parts:
                mime = part.get("mimeType", "")
                if mime == "text/html" and not body_html:
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body_html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                elif mime == "text/plain" and not body_plain:
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body_plain = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                elif part.get("filename"):
                    # Attachment
                    attachments.append({
                        "filename": part["filename"],
                        "mime_type": mime,
                        "size": part.get("body", {}).get("size", 0),
                        "gmail_attachment_id": part.get("body", {}).get("attachmentId"),
                    })
                # Recurse into nested parts
                if part.get("parts"):
                    _walk_parts(part["parts"])

        if payload.get("parts"):
            _walk_parts(payload["parts"])
        else:
            # Single-part message
            mime = payload.get("mimeType", "")
            data = payload.get("body", {}).get("data", "")
            if data:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if mime == "text/html":
                    body_html = decoded
                else:
                    body_plain = decoded

        # Convert to markdown — strip quoted replies for cleaner display
        body_markdown = None
        body_quoted = None
        if body_html:
            # Strip Gmail quoted content before converting
            new_html, quoted_html = _split_html_quote(body_html)
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0  # No wrapping
            body_markdown = h.handle(new_html).strip()
            if quoted_html:
                body_quoted = h.handle(quoted_html).strip()
        elif body_plain:
            # Use email-reply-parser for plain text
            new_text, quoted_text = _split_plain_quote(body_plain)
            body_markdown = new_text.strip() if new_text else body_plain.strip()
            body_quoted = quoted_text.strip() if quoted_text else None

        # Clean up markdown for readability
        if body_markdown:
            body_markdown = _clean_email_markdown(body_markdown)
        # Append quoted content with a marker the UI can detect
        if body_quoted:
            body_quoted = _clean_email_markdown(body_quoted)
            body_markdown = (body_markdown or "") + "\n\n<!-- quoted -->\n" + body_quoted

        return {
            "body_html": body_html,
            "body_markdown": body_markdown,
            "body_quoted": body_quoted if body_quoted else None,
            "attachments_json": attachments or None,
            "sender_name": sender_name,
            "all_recipients": all_recipients or None,
        }
    except Exception as e:
        logger.warning(f"Failed to fetch content for message {message_id}: {e}")
        return None


def process_message(
    session,
    service,
    user_email: str,
    message_id: str,
    domain_index: dict,
    dry_run: bool = False,
) -> str:
    """Process a single message through the matching pipeline.

    Returns: 'tier1' | 'tier2' | 'tier3' | 'skip' | 'error'
    """
    msg_meta = extract_message_metadata(service, message_id)
    if not msg_meta:
        return "error"

    # Skip promotional, social, spam, and forum emails — they shouldn't match entities
    _SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM", "TRASH"}
    if _SKIP_LABELS & set(msg_meta.get("labelIds", [])):
        return "skip"

    thread_id = msg_meta["threadId"]
    subject = msg_meta["subject"]
    from_addr = extract_email_address(msg_meta["from"])
    external_addresses = extract_all_addresses(msg_meta)
    direction = determine_direction(user_email, from_addr)

    # --- Tier 1: ID match ---
    # Check if this exact message is already tracked
    existing_msg = session.query(EmailTracking).filter(
        EmailTracking.gmail_message_id == msg_meta["id"]
    ).first()
    if existing_msg:
        # Already tracked — nothing to do
        return "tier1"

    # Check if this thread is tracked (matches an existing record)
    existing_thread = session.query(EmailTracking).filter(
        EmailTracking.gmail_thread_id == thread_id
    ).first()
    if existing_thread:
        if dry_run:
            logger.info(
                f"  [T1] Thread match: thread={thread_id}, direction={direction}, "
                f"subject='{subject[:60]}'"
            )
        else:
            # Draft → sent transition: update original record
            if existing_thread.direction == "draft" and direction == "sent" and existing_thread.gmail_message_id is None:
                existing_thread.direction = "sent"
                existing_thread.gmail_message_id = msg_meta["id"]
                existing_thread.sent_at = msg_meta.get("date") or datetime.now(timezone.utc)
                existing_thread.sent_confirmed = True
                existing_thread.updated_at = datetime.now(timezone.utc)
                # Fetch body content for the sent message
                content = fetch_message_content(service, msg_meta["id"])
                if content:
                    existing_thread.body_markdown = content["body_markdown"]
                    existing_thread.body_html = content["body_html"]
                    existing_thread.attachments_json = content["attachments_json"]
                    existing_thread.sender_name = content["sender_name"]
                    existing_thread.all_recipients = content["all_recipients"]
            else:
                # New message on tracked thread — create a new row inheriting the RFQ link
                if direction == "received":
                    recipient = from_addr
                else:
                    recipient = extract_email_address(msg_meta.get("to", ""))
                # Fetch body content
                content = fetch_message_content(service, msg_meta["id"])
                tracking = EmailTracking(
                    gmail_thread_id=thread_id,
                    gmail_message_id=msg_meta["id"],
                    user_email=user_email,
                    rfq_id=existing_thread.rfq_id,
                    rfq_token=existing_thread.rfq_token,
                    opportunity_id=existing_thread.opportunity_id,
                    supplier_id=existing_thread.supplier_id,
                    customer_id=existing_thread.customer_id,
                    match_type=existing_thread.match_type,
                    direction=direction,
                    subject=subject,
                    recipient_email=recipient,
                    sent_at=msg_meta.get("date"),
                    body_markdown=content["body_markdown"] if content else None,
                    body_html=content["body_html"] if content else None,
                    attachments_json=content["attachments_json"] if content else None,
                    sender_name=content["sender_name"] if content else None,
                    all_recipients=content["all_recipients"] if content else None,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(tracking)
        return "tier1"

    # --- Tier 2: Subject pattern match ---
    subject_match = match_by_subject(subject)
    if subject_match:
        rfq_number = subject_match.get("rfq_number")
        op_number = subject_match.get("opportunity_number")

        # Verify RFQ exists in DB (rfq_number in DB is 'RFQ-2026-0032' format)
        rfq = None
        full_rfq_number = None
        if rfq_number:
            full_rfq_number = f"RFQ-{rfq_number}" if not rfq_number.upper().startswith("RFQ-") else rfq_number
            rfq = session.query(RFQ).filter(RFQ.rfq_number == full_rfq_number).first()

        # If no RFQ found by number, try matching by NetSuite Opportunity ID
        if not rfq and op_number:
            rfq = session.query(RFQ).filter(RFQ.netsuite_opportunity == op_number).first()
            if rfq:
                full_rfq_number = rfq.rfq_number

        if rfq or op_number:
            # Also run Tier 3 contact matching to identify supplier/customer
            contact_match = match_by_contact(session, external_addresses, domain_index)
            if dry_run:
                logger.info(
                    f"  [T2] Subject match: '{subject}' → "
                    f"rfq={rfq_number}, op={op_number}, direction={direction}, "
                    f"supplier={contact_match.get('supplier_id')}"
                )
            else:
                if direction == "received":
                    recipient = from_addr
                else:
                    recipient = extract_email_address(msg_meta.get("to", ""))
                # Fetch body content for RFQ-linked emails
                content = fetch_message_content(service, msg_meta["id"])
                tracking = EmailTracking(
                    gmail_thread_id=thread_id,
                    gmail_message_id=msg_meta["id"],
                    user_email=user_email,
                    rfq_id=full_rfq_number if rfq else None,
                    rfq_token=full_rfq_number,
                    opportunity_id=op_number,
                    supplier_id=contact_match.get("supplier_id"),
                    customer_id=contact_match.get("customer_id"),
                    match_type=contact_match.get("match_type"),
                    direction=direction,
                    subject=subject,
                    recipient_email=recipient,
                    sent_at=msg_meta.get("date"),
                    body_markdown=content["body_markdown"] if content else None,
                    body_html=content["body_html"] if content else None,
                    attachments_json=content["attachments_json"] if content else None,
                    sender_name=content["sender_name"] if content else None,
                    all_recipients=content["all_recipients"] if content else None,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(tracking)
            return "tier2"

    # --- Tier 3: Contact/domain match ---
    contact_match = match_by_contact(session, external_addresses, domain_index)
    if contact_match["match_type"]:
        if dry_run:
            logger.info(
                f"  [T3] {contact_match['match_type']} match: "
                f"supplier={contact_match['supplier_id']}, "
                f"customer={contact_match['customer_id']}, "
                f"direction={direction}, subject='{subject[:60]}'"
            )
        else:
            if direction == "received":
                recipient = from_addr
            else:
                recipient = extract_email_address(msg_meta.get("to", ""))
            tracking = EmailTracking(
                gmail_thread_id=thread_id,
                gmail_message_id=msg_meta["id"],
                user_email=user_email,
                direction=direction,
                subject=subject,
                recipient_email=recipient,
                sent_at=msg_meta.get("date"),
                supplier_id=contact_match["supplier_id"],
                customer_id=contact_match["customer_id"],
                match_type=contact_match["match_type"],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(tracking)
        return "tier3"

    return "skip"


def sync_mailbox(session, user_email: str, domain_index: dict, dry_run: bool = False) -> dict:
    """Run incremental sync for a single mailbox.

    Returns:
        dict with counts: {tier1, tier2, tier3, skipped, errors, new_history_id}
    """
    cursor = get_cursor(session, user_email)
    if cursor is None:
        logger.info(f"No cursor for {user_email} — auto-initializing")
        cursor = initialize_cursor(session, user_email, dry_run)
        if cursor is None:
            return {"error": "init_failed"}
        # First run after init: cursor points to current state, nothing to process yet
        return {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0, "initialized": True}

    service = get_gmail_client(user_email)
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0}
    new_history_id = cursor
    seen_message_ids = set()

    _SKIP_LABEL_IDS = {"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}

    try:
        # Fetch history delta
        page_token = None
        while True:
            kwargs = {
                "userId": "me",
                "startHistoryId": cursor,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.users().history().list(**kwargs).execute()
            history_records = response.get("history", [])
            new_history_id = int(response.get("historyId", cursor))

            for record in history_records:
                for msg_added in record.get("messagesAdded", []):
                    msg = msg_added.get("message", {})
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_message_ids:
                        continue

                    # Skip messages with labels indicating spam/trash/promo
                    # (history response includes labelIds, avoids 404s on deleted msgs)
                    msg_labels = set(msg.get("labelIds", []))
                    if msg_labels & _SKIP_LABEL_IDS:
                        continue

                    seen_message_ids.add(msg_id)

                    result = process_message(
                        session, service, user_email, msg_id, domain_index, dry_run
                    )
                    counts[result] = counts.get(result, 0) + 1

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except Exception as e:
        error_msg = str(e)
        # historyId too old — re-initialize
        if "notFound" in error_msg or "historyId" in error_msg.lower():
            logger.warning(f"History expired for {user_email}, re-initializing cursor")
            initialize_cursor(session, user_email, dry_run)
            return {"error": "history_expired", "reinitialized": True}
        logger.error(f"Error syncing {user_email}: {e}")
        counts["errors"] += 1

    # Update cursor
    if not dry_run and new_history_id > cursor:
        update_cursor(session, user_email, new_history_id)
        session.commit()

    counts["new_history_id"] = new_history_id
    return counts


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail mailboxes and match emails")
    parser.add_argument("--init", action="store_true", help="Initialize sync cursors for all enabled mailboxes")
    parser.add_argument("--user", type=str, help="Scan only a specific user's mailbox")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without writing to database")
    args = parser.parse_args()

    session = get_session()

    try:
        if args.user:
            mailboxes = [args.user]
        else:
            mailboxes = get_enabled_mailboxes(session)

        if not mailboxes:
            logger.warning("No enabled mailboxes found. Add entries to mailbox_scan_config.")
            return

        logger.info(f"Processing {len(mailboxes)} mailbox(es)")

        if args.init:
            for email in mailboxes:
                initialize_cursor(session, email, args.dry_run)
            if not args.dry_run:
                session.commit()
            logger.info("Cursor initialization complete")
            return

        # Build domain index once (shared across all mailboxes)
        logger.info("Building domain index...")
        domain_index = build_domain_index(session)
        logger.info(f"Domain index: {len(domain_index)} domains mapped")

        # Sync each mailbox
        total = {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0}
        for email in mailboxes:
            logger.info(f"Syncing {email}...")
            result = sync_mailbox(session, email, domain_index, args.dry_run)
            if "error" in result:
                logger.warning(f"  {email}: {result}")
                continue
            for key in ("tier1", "tier2", "tier3", "skipped", "errors"):
                total[key] += result.get(key, 0)
            logger.info(
                f"  {email}: T1={result.get('tier1',0)} T2={result.get('tier2',0)} "
                f"T3={result.get('tier3',0)} skip={result.get('skipped',0)}"
            )

        logger.info(
            f"TOTAL: T1={total['tier1']} T2={total['tier2']} T3={total['tier3']} "
            f"skipped={total['skipped']} errors={total['errors']}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
