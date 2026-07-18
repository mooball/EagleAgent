"""Scan for duplicate suppliers.

Finds suppliers with no netsuite_id that likely duplicate an existing
netsuite-linked supplier. Uses domain matching + trigram name similarity.

Can be run standalone or called from the admin panel.
"""

import logging
from urllib.parse import urlparse

from sqlalchemy import func, or_, literal
from sqlalchemy.orm import Session

from includes.dashboard.models import Supplier
from includes.dashboard.database import _extract_domain

logger = logging.getLogger(__name__)

# Two-pass matching thresholds
TRIGRAM_THRESHOLD = 0.45  # Broad initial filter
HIGH_CONFIDENCE_THRESHOLD = 0.7  # Name similarity alone is enough


def _get_supplier_domains(supplier) -> set[str]:
    """Extract all domains associated with a supplier (url + alt_domains + contacts)."""
    domains = set()
    if supplier.url:
        d = _extract_domain(supplier.url)
        if d:
            domains.add(d)
    for alt in (supplier.alt_domains or []):
        d = _extract_domain(alt) if alt.startswith("http") else alt.lower().strip()
        if d:
            domains.add(d)
    for c in (supplier.contacts or []):
        if isinstance(c, dict) and c.get("url"):
            d = _extract_domain(c["url"])
            if d:
                domains.add(d)
    return domains


def scan_duplicates(session: Session) -> list[dict]:
    """Find potential duplicate suppliers.

    Returns a list of dicts:
    {
        "new_supplier": {id, name, url, source, ...},
        "match": {id, name, netsuite_id, url, ...},
        "confidence": float (0-1),
        "reasons": ["domain_match", "name_similarity:0.85", ...],
    }
    """
    # Get all non-netsuite suppliers (the "new" ones)
    new_suppliers = (
        session.query(Supplier)
        .filter(Supplier.netsuite_id.is_(None))
        .all()
    )

    # Filter out previously dismissed suppliers
    new_suppliers = [
        s for s in new_suppliers
        if "__dedup_reviewed__" not in (s.alt_names or [])
    ]

    if not new_suppliers:
        return []

    # Get all netsuite-linked suppliers for comparison
    netsuite_suppliers = (
        session.query(Supplier)
        .filter(Supplier.netsuite_id.isnot(None))
        .all()
    )

    if not netsuite_suppliers:
        return []

    # Pre-compute domains for netsuite suppliers
    ns_domain_index: dict[str, list] = {}  # domain -> [supplier, ...]
    for ns in netsuite_suppliers:
        for domain in _get_supplier_domains(ns):
            ns_domain_index.setdefault(domain, []).append(ns)

    # Pre-compute alt_names lookup (lowercased)
    ns_altname_index: dict[str, list] = {}  # alt_name_lower -> [supplier, ...]
    for ns in netsuite_suppliers:
        for alt in (ns.alt_names or []):
            ns_altname_index.setdefault(alt.lower().strip(), []).append(ns)

    results = []

    for new_sup in new_suppliers:
        best_match = None
        best_confidence = 0.0
        best_reasons = []

        new_name_lower = (new_sup.name or "").strip().lower()
        new_domains = _get_supplier_domains(new_sup)

        # Pass 1: Domain match
        domain_matches = set()
        for d in new_domains:
            for ns in ns_domain_index.get(d, []):
                domain_matches.add(ns)

        if domain_matches:
            # Domain match is high confidence; pick best name similarity
            for ns in domain_matches:
                ns_name_lower = (ns.name or "").strip().lower()
                # Compute name similarity via simple ratio
                sim = _name_similarity(new_name_lower, ns_name_lower)
                confidence = max(0.8, sim)  # Domain match is always >= 0.8
                reasons = [f"domain_match"]
                if sim > 0.5:
                    reasons.append(f"name_similarity:{sim:.2f}")
                if confidence > best_confidence:
                    best_match = ns
                    best_confidence = confidence
                    best_reasons = reasons

        # Pass 2: Alt-names exact match
        if not best_match and new_name_lower in ns_altname_index:
            candidates = ns_altname_index[new_name_lower]
            best_match = candidates[0]
            best_confidence = 0.9
            best_reasons = ["alt_name_exact_match"]

        # Pass 3: Name containment
        if not best_match:
            for ns in netsuite_suppliers:
                ns_name_lower = (ns.name or "").strip().lower()
                if not ns_name_lower or not new_name_lower:
                    continue
                if ns_name_lower in new_name_lower or new_name_lower in ns_name_lower:
                    # Containment — high confidence if lengths are close
                    len_ratio = min(len(ns_name_lower), len(new_name_lower)) / max(len(ns_name_lower), len(new_name_lower))
                    confidence = 0.6 + (len_ratio * 0.3)
                    if confidence > best_confidence:
                        best_match = ns
                        best_confidence = confidence
                        best_reasons = [f"name_containment", f"len_ratio:{len_ratio:.2f}"]

        # Pass 4: Trigram similarity (pg_trgm via Python fallback)
        if not best_match:
            for ns in netsuite_suppliers:
                ns_name_lower = (ns.name or "").strip().lower()
                if not ns_name_lower:
                    continue
                sim = _name_similarity(new_name_lower, ns_name_lower)
                if sim >= HIGH_CONFIDENCE_THRESHOLD and sim > best_confidence:
                    best_match = ns
                    best_confidence = sim
                    best_reasons = [f"name_similarity:{sim:.2f}"]

        if best_match and best_confidence >= 0.6:
            results.append({
                "new_supplier": _supplier_summary(new_sup),
                "match": _supplier_summary(best_match),
                "confidence": round(best_confidence, 3),
                "reasons": best_reasons,
            })

    # Sort by confidence descending
    results.sort(key=lambda r: -r["confidence"])
    return results


def _name_similarity(a: str, b: str) -> float:
    """Compute trigram-like similarity between two strings."""
    if not a or not b:
        return 0.0
    # Use difflib SequenceMatcher as a proxy for trigram similarity
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def scan_internal_duplicates(session: Session) -> list[dict]:
    """Find duplicates among non-netsuite (web-added) suppliers themselves.

    Compares every non-netsuite supplier against every other non-netsuite
    supplier. Groups them so only the best match per cluster is returned.

    Returns a list of dicts:
    {
        "new_supplier": {id, name, url, source, ...},  -- the one to remove
        "match": {id, name, url, source, ...},          -- the one to keep
        "confidence": float (0-1),
        "reasons": [...],
    }
    """
    new_suppliers = (
        session.query(Supplier)
        .filter(Supplier.netsuite_id.is_(None))
        .all()
    )

    # Filter out previously dismissed
    new_suppliers = [
        s for s in new_suppliers
        if "__dedup_reviewed__" not in (s.alt_names or [])
    ]

    if len(new_suppliers) < 2:
        return []

    # Pre-compute domains for all
    domain_index: dict[str, list] = {}  # domain -> [supplier, ...]
    for sup in new_suppliers:
        for domain in _get_supplier_domains(sup):
            domain_index.setdefault(domain, []).append(sup)

    # Track which suppliers have already been matched (avoid dupes in results)
    matched_ids: set[str] = set()
    results = []

    for i, sup_a in enumerate(new_suppliers):
        if str(sup_a.id) in matched_ids:
            continue

        a_name_lower = (sup_a.name or "").strip().lower()
        a_domains = _get_supplier_domains(sup_a)

        best_match = None
        best_confidence = 0.0
        best_reasons = []

        for j, sup_b in enumerate(new_suppliers):
            if i >= j:
                continue  # only compare forward to avoid double-counting
            if str(sup_b.id) in matched_ids:
                continue

            b_name_lower = (sup_b.name or "").strip().lower()
            b_domains = _get_supplier_domains(sup_b)

            confidence = 0.0
            reasons = []

            # Check domain overlap
            shared_domains = a_domains & b_domains
            if shared_domains:
                sim = _name_similarity(a_name_lower, b_name_lower)
                confidence = max(0.8, sim)
                reasons.append("domain_match")
                if sim > 0.5:
                    reasons.append(f"name_similarity:{sim:.2f}")

            # Exact name match
            if not reasons and a_name_lower and a_name_lower == b_name_lower:
                confidence = 1.0
                reasons.append("exact_name_match")

            # Name containment
            if not reasons and a_name_lower and b_name_lower:
                if a_name_lower in b_name_lower or b_name_lower in a_name_lower:
                    len_ratio = min(len(a_name_lower), len(b_name_lower)) / max(len(a_name_lower), len(b_name_lower))
                    if len_ratio > 0.6:
                        confidence = 0.6 + (len_ratio * 0.3)
                        reasons.append("name_containment")
                        reasons.append(f"len_ratio:{len_ratio:.2f}")

            # Name similarity
            if not reasons and a_name_lower and b_name_lower:
                sim = _name_similarity(a_name_lower, b_name_lower)
                if sim >= HIGH_CONFIDENCE_THRESHOLD:
                    confidence = sim
                    reasons.append(f"name_similarity:{sim:.2f}")

            if confidence > best_confidence:
                best_match = sup_b
                best_confidence = confidence
                best_reasons = reasons

        if best_match and best_confidence >= 0.6:
            # Pick the "keep" supplier: prefer the one with more data (url, contacts, etc.)
            keep, remove = _pick_keep_remove(sup_a, best_match)
            matched_ids.add(str(remove.id))
            results.append({
                "new_supplier": _supplier_summary(remove),
                "match": _supplier_summary(keep),
                "confidence": round(best_confidence, 3),
                "reasons": best_reasons,
            })

    results.sort(key=lambda r: -r["confidence"])
    return results


def _pick_keep_remove(a, b):
    """Decide which supplier to keep and which to remove.

    Prefer the one with: netsuite_id > more contacts > has url > earlier created.
    """
    def _score(sup):
        s = 0
        if sup.netsuite_id:
            s += 100
        if sup.url:
            s += 10
        s += len(sup.contacts or []) * 2
        s += len(sup.alt_names or [])
        s += len(sup.alt_domains or [])
        return s

    score_a = _score(a)
    score_b = _score(b)
    if score_a >= score_b:
        return a, b
    return b, a


def _supplier_summary(sup) -> dict:
    """Convert a Supplier ORM object to a summary dict for the UI."""
    contacts = sup.contacts if isinstance(sup.contacts, list) else []
    # Extract first URL from contacts
    contact_url = None
    for c in contacts:
        if isinstance(c, dict) and c.get("url"):
            contact_url = c["url"]
            break
    scp = sup.supply_chain_position or {}
    return {
        "id": str(sup.id),
        "name": sup.name,
        "netsuite_id": sup.netsuite_id,
        "url": sup.url,
        "contact_url": contact_url,
        "country": sup.country,
        "tier": scp.get("tier"),
        "source": sup.source,
        "alt_names": sup.alt_names or [],
        "alt_domains": sup.alt_domains or [],
    }


def merge_supplier(session: Session, keep_id: str, remove_id: str, merge_fields: dict | None = None) -> dict:
    """Merge remove_supplier into keep_supplier, then delete the removed one.

    merge_fields: optional dict of fields to pull from the removed supplier
                  e.g. {"url": True, "contacts": True}

    Returns {"status": "ok", "updated_rfq_items": N} or {"status": "error", "message": ...}
    """
    import uuid
    from includes.dashboard.models import RFQItem, SupplierBrand
    from sqlalchemy.orm.attributes import flag_modified

    keep_uuid = uuid.UUID(keep_id)
    remove_uuid = uuid.UUID(remove_id)

    keep_sup = session.query(Supplier).filter(Supplier.id == keep_uuid).first()
    remove_sup = session.query(Supplier).filter(Supplier.id == remove_uuid).first()

    if not keep_sup or not remove_sup:
        return {"status": "error", "message": "Supplier not found."}

    # 1. Merge selected fields from remove into keep
    merge_fields = merge_fields or {}
    if merge_fields.get("url") and remove_sup.url:
        # Add the removed supplier's URL as an alt_domain (not replace)
        domain = _extract_domain(remove_sup.url)
        if domain:
            alt_domains = list(keep_sup.alt_domains or [])
            if domain not in alt_domains:
                alt_domains.append(domain)
                keep_sup.alt_domains = alt_domains
                flag_modified(keep_sup, "alt_domains")
        # If keep_sup has no URL at all, use the removed one
        if not keep_sup.url:
            keep_sup.url = remove_sup.url
    if merge_fields.get("contacts") and remove_sup.contacts:
        # Merge contact lists, dedup by url/email
        existing_keys = set()
        for c in (keep_sup.contacts or []):
            if isinstance(c, dict):
                existing_keys.add(c.get("url", "") + "|" + c.get("email", ""))
        merged_contacts = list(keep_sup.contacts or [])
        for c in (remove_sup.contacts or []):
            if isinstance(c, dict):
                key = c.get("url", "") + "|" + c.get("email", "")
                if key not in existing_keys:
                    merged_contacts.append(c)
                    existing_keys.add(key)
        keep_sup.contacts = merged_contacts
        flag_modified(keep_sup, "contacts")

    # Add removed supplier's name as alt_name on the kept supplier
    alt_names = list(keep_sup.alt_names or [])
    if remove_sup.name and remove_sup.name not in alt_names and remove_sup.name.lower() != keep_sup.name.lower():
        alt_names.append(remove_sup.name)
        keep_sup.alt_names = alt_names
        flag_modified(keep_sup, "alt_names")

    # Merge alt_domains
    if remove_sup.url:
        domain = _extract_domain(remove_sup.url)
        if domain:
            alt_domains = list(keep_sup.alt_domains or [])
            if domain not in alt_domains:
                alt_domains.append(domain)
                keep_sup.alt_domains = alt_domains
                flag_modified(keep_sup, "alt_domains")

    # 2. Move supplier_brands links
    existing_brand_ids = set(
        r[0] for r in session.query(SupplierBrand.brand_id)
        .filter(SupplierBrand.supplier_id == keep_uuid).all()
    )
    orphan_brands = (
        session.query(SupplierBrand)
        .filter(SupplierBrand.supplier_id == remove_uuid)
        .all()
    )
    for sb in orphan_brands:
        if sb.brand_id in existing_brand_ids:
            session.delete(sb)
        else:
            sb.supplier_id = keep_uuid

    # 3. Update RFQ items referencing the removed supplier
    updated_rfq_items = 0
    rfq_items = session.query(RFQItem).filter(
        RFQItem.suppliers.isnot(None)
    ).all()
    for item in rfq_items:
        suppliers = item.suppliers or []
        changed = False
        for sup_entry in suppliers:
            if sup_entry.get("supplier_id") == str(remove_uuid):
                sup_entry["supplier_id"] = str(keep_uuid)
                # Keep the name from the netsuite supplier
                sup_entry["name"] = keep_sup.name
                changed = True
        if changed:
            item.suppliers = suppliers
            flag_modified(item, "suppliers")
            updated_rfq_items += 1

    # Also update brand_suppliers JSONB references
    brand_items = session.query(RFQItem).filter(
        RFQItem.brand_suppliers.isnot(None)
    ).all()
    for item in brand_items:
        brand_sups = item.brand_suppliers or []
        changed = False
        for sup_entry in brand_sups:
            if sup_entry.get("supplier_id") == str(remove_uuid):
                sup_entry["supplier_id"] = str(keep_uuid)
                sup_entry["name"] = keep_sup.name
                changed = True
        if changed:
            item.brand_suppliers = brand_sups
            flag_modified(item, "brand_suppliers")

    # 4. Reassign email_tracking records from remove -> keep
    from includes.dashboard.models import EmailTracking, Contact, Transaction
    email_count = (
        session.query(EmailTracking)
        .filter(EmailTracking.supplier_id == remove_uuid)
        .update({"supplier_id": keep_uuid}, synchronize_session=False)
    )
    if email_count:
        logger.info(f"Reassigned {email_count} email_tracking rows from {remove_id} to {keep_id}")

    # 5. Reassign contact records from remove -> keep
    contact_count = (
        session.query(Contact)
        .filter(Contact.supplier_id == remove_uuid)
        .update({"supplier_id": keep_uuid}, synchronize_session=False)
    )
    if contact_count:
        logger.info(f"Reassigned {contact_count} contacts from {remove_id} to {keep_id}")

    # 6. Reassign product_suppliers (Transaction) records from remove -> keep
    txn_count = (
        session.query(Transaction)
        .filter(Transaction.supplier_id == remove_uuid)
        .update({"supplier_id": keep_uuid}, synchronize_session=False)
    )
    if txn_count:
        logger.info(f"Reassigned {txn_count} product_suppliers rows from {remove_id} to {keep_id}")

    # 7. Delete the removed supplier
    session.delete(remove_sup)

    return {"status": "ok", "updated_rfq_items": updated_rfq_items}
