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
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.com.au',
    'hotmail.com', 'outlook.com', 'live.com', 'msn.com',
    'icloud.com', 'me.com', 'mac.com', 'aol.com',
    'protonmail.com', 'proton.me', 'mail.com', 'zoho.com',
})

# Internal company domains — never save these as entity domains
_INTERNAL_DOMAINS = frozenset({
    'eagle-exports.com', 'eagle-exports.com.au',
    'eaglexp.com', 'eaglexp.com.au',
})


def find_sender_match(
    session: Session,
    sender_email: str,
    domain_index: dict[str, list[dict]] | None = None,
) -> dict | None:
    """Look up a sender email against known contacts, customers, and domains.

    Uses the same exact-match → domain-fallback strategy as the automated
    Gmail sync pipeline.  Returns a single definitive match or None.

    Returns:
        {
            "type": "customer" | "supplier",
            "id": UUID,
            "name": str,
            "match_type": "exact" | "domain",
        }
        or None if no match.
    """
    if not sender_email:
        return None

    email_lower = sender_email.lower().strip()

    # Step 1: Exact match on contact email or customer email
    contact = (
        session.query(Contact)
        .filter(
            func.lower(Contact.email) == email_lower,
            Contact.isinactive == False,
        )
        .first()
    )
    if contact and (contact.supplier_id or contact.customer_id):
        if contact.supplier_id:
            supplier = session.query(Supplier).get(contact.supplier_id)
            return {
                "type": "supplier",
                "id": contact.supplier_id,
                "name": supplier.name if supplier else "Supplier",
                "match_type": "exact",
            }
        else:
            customer = session.query(Customer).get(contact.customer_id)
            return {
                "type": "customer",
                "id": contact.customer_id,
                "name": customer.companyname if customer else "Customer",
                "match_type": "exact",
            }

    customer = (
        session.query(Customer)
        .filter(
            func.lower(Customer.email) == email_lower,
            Customer.isinactive == False,
        )
        .first()
    )
    if customer:
        return {
            "type": "customer",
            "id": customer.id,
            "name": customer.companyname,
            "match_type": "exact",
        }

    # Step 2: Domain fallback
    domain = extract_domain(sender_email)
    if domain and domain not in _GENERIC_DOMAINS:
        if domain_index is None:
            domain_index = build_domain_index(session)
        entries = domain_index.get(domain.lower())
        if entries:
            entry = entries[0]  # first match only — no ambiguity
            # Resolve name if not already in the index
            name = entry.get("name")
            if not name and entry["type"] == "supplier":
                supplier = session.query(Supplier).get(entry["id"])
                name = supplier.name if supplier else "Supplier"
            elif not name and entry["type"] == "customer":
                customer = session.query(Customer).get(entry["id"])
                name = customer.companyname if customer else "Customer"
            return {
                "type": entry["type"],
                "id": entry["id"],
                "name": name or entry["type"].capitalize(),
                "match_type": "domain",
            }

    return None


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


def match_by_contact(
    session: Session,
    email_addresses: list[str],
    domain_index: dict[str, list[dict]],
) -> dict:
    """Tier 3: Match sender/recipients to known contacts or domains.

    Returns:
        dict with keys:
            - match_type: 'exact' | 'domain' | None
            - supplier_id: UUID | None
            - customer_id: UUID | None
    """
    if not email_addresses:
        return {"match_type": None, "supplier_id": None, "customer_id": None}

    # Step A: Exact email match
    for email in email_addresses:
        email_lower = email.lower().strip()

        # Check contacts table
        contact = session.query(Contact).filter(
            func.lower(Contact.email) == email_lower,
            Contact.isinactive == False,
        ).first()
        if contact and (contact.supplier_id or contact.customer_id):
            return {
                "match_type": "exact",
                "supplier_id": contact.supplier_id,
                "customer_id": contact.customer_id,
            }

        # Check customer email
        customer = session.query(Customer).filter(
            func.lower(Customer.email) == email_lower,
            Customer.isinactive == False,
        ).first()
        if customer:
            return {
                "match_type": "exact",
                "supplier_id": None,
                "customer_id": customer.id,
            }

    # Step B: Domain fallback
    for email in email_addresses:
        domain = extract_domain(email)
        if not domain or domain in _GENERIC_DOMAINS:
            continue
        entries = domain_index.get(domain)
        if entries:
            # Take first match (could be multiple — pick first)
            entry = entries[0]
            return {
                "match_type": "domain",
                "supplier_id": entry["id"] if entry["type"] == "supplier" else None,
                "customer_id": entry["id"] if entry["type"] == "customer" else None,
            }

    return {"match_type": None, "supplier_id": None, "customer_id": None}


def determine_direction(user_email: str, from_address: str) -> str:
    """Determine email direction based on sender vs. scanned mailbox owner."""
    if from_address and from_address.lower().strip() == user_email.lower().strip():
        return "sent"
    return "received"
