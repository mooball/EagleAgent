"""Gmail mailbox scanning — email matching pipeline.

Implements the three-tier matching strategy:
  Tier 1: ID match (gmail_thread_id, gmail_message_id, gmail_draft_id)
  Tier 2: Subject pattern match (RFQ-<id>, OP<number>)
  Tier 3: Contact/domain match (exact email → domain fallback)
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from includes.dashboard.models import (
    Contact,
    Customer,
    EmailTracking,
    RFQ,
    Supplier,
)

logger = logging.getLogger(__name__)

# Patterns for subject line matching
# Matches RFQ-2026-0032, RFQ-12345, [RFQ-2026-0032], etc.
RFQ_PATTERN = re.compile(r'\bRFQ[-‑–]([\d][\d\-]+\d)\b', re.IGNORECASE)
OP_PATTERN = re.compile(r'\bOP(\d+)\b', re.IGNORECASE)


def extract_domain(email: str) -> Optional[str]:
    """Extract domain from an email address, lowercased."""
    if not email or '@' not in email:
        return None
    return email.rsplit('@', 1)[1].lower().strip()


def _extract_email_addr(raw: str) -> str:
    """Extract a plain email address from Gmail format strings.

    Gmail returns sender/recipient in formats like:
        "Name" <email@domain.com>
        email@domain.com
        Name <email@domain.com>  (no quotes)

    Returns the bare email address stripped of whitespace.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Match 'Name <email>' or '"Name" <email>' pattern
    match = re.match(r'(?:"[^"]*"|[^<]*)\s*<([^>]+)>', raw)
    if match:
        return match.group(1).strip()
    return raw


def extract_domain_from_url(url: str) -> Optional[str]:
    """Extract root domain from a URL (strips scheme, path, www prefix)."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return None
        host = host.lower()
        if host.startswith('www.'):
            host = host[4:]
        return host
    except Exception:
        return None


def build_domain_index(session: Session) -> dict[str, list[dict]]:
    """Build a domain → entity mapping from contacts, customers, and supplier URLs.

    Returns:
        dict mapping domain (str) to list of dicts:
        [{"type": "supplier"|"customer", "id": UUID, "name": str}, ...]
    """
    domain_map: dict[str, list[dict]] = {}

    def _add(domain: str, entity_type: str, entity_id, name: str):
        if not domain:
            return
        domain = domain.lower()
        # Skip generic email domains
        if domain in _GENERIC_DOMAINS:
            return
        entry = {"type": entity_type, "id": entity_id, "name": name}
        domain_map.setdefault(domain, []).append(entry)

    # 1. Contact emails → supplier/customer
    contacts = session.query(
        Contact.email, Contact.supplier_id, Contact.customer_id
    ).filter(Contact.email.isnot(None), Contact.isinactive == False).all()

    for c in contacts:
        domain = extract_domain(c.email)
        if c.supplier_id:
            _add(domain, "supplier", c.supplier_id, None)
        elif c.customer_id:
            _add(domain, "customer", c.customer_id, None)

    # 2. Customer emails
    customers = session.query(Customer.id, Customer.companyname, Customer.email).filter(
        Customer.email.isnot(None), Customer.isinactive == False
    ).all()
    for cust in customers:
        domain = extract_domain(cust.email)
        _add(domain, "customer", cust.id, cust.companyname)

    # 3. Supplier website URLs
    suppliers = session.query(Supplier.id, Supplier.name, Supplier.url).filter(
        Supplier.url.isnot(None)
    ).all()
    for s in suppliers:
        domain = extract_domain_from_url(s.url)
        _add(domain, "supplier", s.id, s.name)

    # 4. Supplier alt_domains
    suppliers_alt = session.query(Supplier.id, Supplier.name, Supplier.alt_domains).filter(
        Supplier.alt_domains.isnot(None)
    ).all()
    for s in suppliers_alt:
        if isinstance(s.alt_domains, list):
            for d in s.alt_domains:
                _add(d.lower().strip(), "supplier", s.id, s.name)

    return domain_map


# Common email providers — skip domain matching for these
_GENERIC_DOMAINS = frozenset({
    # Global freemail
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.com.au',
    'hotmail.com', 'outlook.com', 'live.com', 'msn.com',
    'icloud.com', 'me.com', 'mac.com', 'aol.com',
    'protonmail.com', 'proton.me', 'mail.com', 'zoho.com',
    # Yahoo country variants
    'yahoo.co.id', 'yahoo.co.in', 'yahoo.co.uk',
    # Yahoo-owned freemail aliases
    'ymail.com', 'y7mail.com', 'rocketmail.com',
    # Microsoft country variants
    'hotmail.com.au', 'live.com.au', 'live.fr', 'outlook.com.au',
    # Chinese freemail
    '163.com', 'qq.com',
    # Korean portal
    'naver.com',
    # Australian consumer ISPs
    'bigpond.com', 'bigpond.com.au', 'bigpond.net.au',
    'optusnet.com.au', 'tpg.com.au', 'iinet.net.au',
    'internode.on.net', 'westnet.com.au', 'dodo.com.au',
    'iprimus.com.au', 'ozemail.com.au', 'exemail.com.au',
    'onthenet.com.au', 'activ8.net.au', 'skymesh.com.au',
    'pacific.net.au',
    # International ISPs
    'orange.fr', 'wanadoo.fr', 'btinternet.com', 'connect.com.fj',
})

# Internal company domains — never save these as entity domains
_INTERNAL_DOMAINS = frozenset({
    'eagle-exports.com', 'eagle-exports.com.au',
    'eaglexp.com', 'eaglexp.com.au',
})


def find_all_matches(
    session: Session,
    sender_email: str,
    domain_index: dict[str, list[dict]] | None = None,
) -> dict:
    """Find ALL candidate entities matching a sender email.

    Steps:
      0. Skip generic/ISP domains entirely
      1. Exact email match against contacts + customers
      2. Domain fallback against domain_index

    Returns:
        {
            "match_type": "exact" | "domain" | None,
            "candidates": [
                {"type": "supplier"|"customer", "id": UUID, "name": str, "match_type": "exact"|"domain"},
                ...
            ],
            "is_unique": bool,   # True if all candidates are the same (type, id)
            "unique_entity": {"type": ..., "id": ..., "name": ..., "match_type": ...} | None,
        }
    """
    empty = {"match_type": None, "candidates": [], "is_unique": False, "unique_entity": None}

    if not sender_email:
        return empty

    email_lower = _extract_email_addr(sender_email).lower()
    if not email_lower or "@" not in email_lower:
        return empty

    # ── Step 0: Skip generic/ISP/internal domains ──────────────────────────
    domain = email_lower.rsplit("@", 1)[1].lower()
    if domain in _GENERIC_DOMAINS or domain in _INTERNAL_DOMAINS:
        logger.debug("Skipping domain: %s", domain)
        return empty

    candidates: list[dict] = []

    def _add(etype: str, eid, ename: str, match_type: str):
        """Add a candidate, deduplicating by (type, id)."""
        eid_str = str(eid)
        for c in candidates:
            if c["type"] == etype and c["id"] == eid_str:
                return
        candidates.append({
            "type": etype,
            "id": eid_str,
            "name": ename or etype.capitalize(),
            "match_type": match_type,
        })

    # ── Step 1: Exact email match — find ALL matches ─────────────────────
    # Contacts with this email
    contacts = (
        session.query(Contact)
        .filter(
            func.lower(Contact.email) == email_lower,
            Contact.isinactive == False,
        )
        .all()
    )
    for c in contacts:
        if c.supplier_id:
            supplier = session.get(Supplier, c.supplier_id)
            if supplier:
                _add("supplier", c.supplier_id, supplier.name, "exact")
        elif c.customer_id:
            customer = session.get(Customer, c.customer_id)
            if customer:
                _add("customer", c.customer_id, customer.companyname, "exact")

    # Customers with this email
    cust_matches = (
        session.query(Customer)
        .filter(
            func.lower(Customer.email) == email_lower,
            Customer.isinactive == False,
        )
        .all()
    )
    for cust in cust_matches:
        _add("customer", cust.id, cust.companyname, "exact")

    if candidates:
        # Check uniqueness: all candidates same type+id?
        unique_ids = set((c["type"], str(c["id"])) for c in candidates)
        is_unique = len(unique_ids) == 1
        return {
            "match_type": "exact",
            "candidates": candidates,
            "is_unique": is_unique,
            "unique_entity": candidates[0] if is_unique else None,
        }

    # ── Step 2: Domain fallback ──────────────────────────────────────────
    if domain_index is None:
        domain_index = build_domain_index(session)

    entries = domain_index.get(domain.lower(), [])
    if not entries:
        return empty

    # Collect all unique entities from domain entries
    for entry in entries:
        ename = entry.get("name")
        if not ename:
            if entry["type"] == "supplier":
                supplier = session.get(Supplier, entry["id"])
                ename = supplier.name if supplier else "Supplier"
            else:
                customer = session.get(Customer, entry["id"])
                ename = customer.companyname if customer else "Customer"
        _add(entry["type"], entry["id"], ename, "domain")

    if not candidates:
        return empty

    unique_ids = set((c["type"], str(c["id"])) for c in candidates)
    is_unique = len(unique_ids) == 1
    return {
        "match_type": "domain",
        "candidates": candidates,
        "is_unique": is_unique,
        "unique_entity": candidates[0] if is_unique else None,
    }


def find_unique_match(
    session: Session,
    sender_email: str,
    domain_index: dict[str, list[dict]] | None = None,
) -> dict | None:
    """Find a single definitive match for a sender email.

    Only returns a match if the sender is UNIQUELY linked to exactly one entity.
    Ambiguous matches (2+ distinct entities) return None — caller should use
    find_all_matches() to present a picker.

    Returns:
        {"type": "supplier"|"customer", "id": UUID, "name": str, "match_type": "exact"|"domain"}
        or None if no unique match.
    """
    result = find_all_matches(session, sender_email, domain_index)
    if result["is_unique"] and result["unique_entity"]:
        return result["unique_entity"]
    return None


# ── Backward-compatible wrapper ──────────────────────────────────────────────
# find_sender_match() is kept for existing callers that expect a single result.
# It now delegates to find_unique_match() for safe matching.
def find_sender_match(
    session: Session,
    sender_email: str,
    domain_index: dict[str, list[dict]] | None = None,
) -> dict | None:
    """Look up a sender email — returns a match only if unambiguous.

    Delegates to find_unique_match().  Ambiguous matches return None.
    """
    return find_unique_match(session, sender_email, domain_index)


def save_sender_domain(
    session: Session,
    sender_email: str,
    entity_type: str,
    entity_id,
) -> str:
    """Save the sender's domain to an entity for future matching.

    - Supplier: Append domain to alt_domains (JSONB) if not present.
    - Customer: Set email field if currently empty.
    - Generic domains (gmail, yahoo, etc.) are skipped.

    Returns a human-readable message describing what was done.
    """
    from sqlalchemy.orm.attributes import flag_modified

    # Parse Gmail format: "Name" <email@domain.com> → email@domain.com
    sender_email = _extract_email_addr(sender_email)
    if not sender_email or "@" not in sender_email:
        return "(no email to extract domain from)"

    domain = sender_email.rsplit("@", 1)[1].lower().strip()

    if domain in _GENERIC_DOMAINS or domain in _INTERNAL_DOMAINS:
        return f"(skipped domain {domain})"

    if entity_type == "supplier":
        supplier = session.query(Supplier).get(entity_id)
        if not supplier:
            return "(supplier not found)"
        alt_domains = list(supplier.alt_domains or [])
        if domain not in alt_domains:
            alt_domains.append(domain)
            supplier.alt_domains = alt_domains
            flag_modified(supplier, "alt_domains")
            logger.info("Saved domain %r to supplier %s", domain, supplier.name)
            return f"Domain '{domain}' saved to {supplier.name}."
        return f"Domain '{domain}' already registered for {supplier.name}."

    elif entity_type == "customer":
        customer = session.query(Customer).get(entity_id)
        if not customer:
            return "(customer not found)"
        if not customer.email:
            customer.email = sender_email.lower().strip()
            logger.info("Saved email %r to customer %s", sender_email, customer.companyname)
            return f"Email '{sender_email}' saved to {customer.companyname}."
        return f"(customer already has email {customer.email}; domain not saved)"

    return ""


def match_by_id(session: Session, gmail_thread_id: str, gmail_message_id: str | None) -> Optional[EmailTracking]:
    """Tier 1: Check if thread/message already exists in email_tracking."""
    filters = [EmailTracking.gmail_thread_id == gmail_thread_id]
    if gmail_message_id:
        filters.append(EmailTracking.gmail_message_id == gmail_message_id)
    return session.query(EmailTracking).filter(or_(*filters)).first()


def match_by_subject(subject: str) -> dict:
    """Tier 2: Extract RFQ/Opportunity IDs from subject line.

    Returns:
        dict with optional keys: rfq_number, opportunity_number
    """
    result = {}
    if not subject:
        return result

    rfq_match = RFQ_PATTERN.search(subject)
    if rfq_match:
        result['rfq_number'] = rfq_match.group(1)

    op_match = OP_PATTERN.search(subject)
    if op_match:
        result['opportunity_number'] = f"OP{op_match.group(1)}"

    return result


def resolve_rfq_from_subject(
    session: Session, subject: str
) -> tuple[Optional["RFQ"], Optional[str], Optional[str]]:
    """Resolve an RFQ row from the subject line (Tier 2 logic).

    Extracts the RFQ/Opportunity number from the subject and looks up the
    RFQ: by rfq_number first, then by NetSuite Opportunity number.

    Shared by the Tier-2 subject match and the Tier-1 fallback for
    no-reply-header messages folded into a tracked thread.

    Returns:
        (rfq, full_rfq_number, op_number)
        - rfq: the RFQ row or None
        - full_rfq_number: 'RFQ-2026-0032'-formatted number or None
        - op_number: 'OP12345'-formatted opportunity number or None
    """
    subject_match = match_by_subject(subject)
    if not subject_match:
        return None, None, None

    rfq_number = subject_match.get("rfq_number")
    op_number = subject_match.get("opportunity_number")

    # Verify the RFQ exists (rfq_number in DB is 'RFQ-2026-0032' format)
    rfq = None
    full_rfq_number = None
    if rfq_number:
        full_rfq_number = (
            f"RFQ-{rfq_number}"
            if not rfq_number.upper().startswith("RFQ-")
            else rfq_number
        )
        rfq = session.query(RFQ).filter(RFQ.rfq_number == full_rfq_number).first()

    # If no RFQ found by number, try matching by NetSuite Opportunity ID
    if not rfq and op_number:
        rfq = session.query(RFQ).filter(RFQ.netsuite_opportunity == op_number).first()
        if rfq:
            full_rfq_number = rfq.rfq_number

    return rfq, full_rfq_number, op_number


def match_by_contact(
    session: Session,
    email_addresses: list[str],
    domain_index: dict[str, list[dict]],
) -> dict:
    """Tier 3: Match sender/recipients to known contacts or domains.

    Only returns a match if the email/domain is UNIQUELY linked to one entity.
    Ambiguous matches (2+ distinct entities) are skipped — they need manual
    resolution via the addon or admin dashboard.

    Returns:
        dict with keys:
            - match_type: 'exact' | 'domain' | None
            - supplier_id: UUID | None
            - customer_id: UUID | None
    """
    if not email_addresses:
        return {"match_type": None, "supplier_id": None, "customer_id": None}

    for email in email_addresses:
        match = find_unique_match(session, email, domain_index)
        if match:
            return {
                "match_type": match["match_type"],
                "supplier_id": match["id"] if match["type"] == "supplier" else None,
                "customer_id": match["id"] if match["type"] == "customer" else None,
            }

    return {"match_type": None, "supplier_id": None, "customer_id": None}


def determine_direction(user_email: str, from_address: str) -> str:
    """Determine email direction based on sender vs. scanned mailbox owner."""
    if from_address and from_address.lower().strip() == user_email.lower().strip():
        return "sent"
    return "received"
