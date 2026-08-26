"""Reusable supplier merge for deduplication.

Shared foundation (S2) for Track A cleanup and Track B filtering. One function:

    merge_suppliers(session, primary_id, duplicate_id, config) -> MergeResult

Rules:
- Every reference to the duplicate is reassigned to the primary.
- Web duplicates are deleted; NetSuite duplicates can never be deleted, so the
  row is kept and `use_instead` points at the primary (the durable "use this
  one instead" state).
- The function never commits — the caller owns the transaction.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm.attributes import flag_modified

from includes.dashboard.database import _extract_domain
from includes.dashboard.models import (
    Contact,
    EmailTracking,
    RFQ,
    RFQItem,
    Supplier,
    SupplierBrand,
    Transaction,
)
from includes.dashboard.supplier_matching import rebuild_match_keys

logger = logging.getLogger(__name__)


@dataclass
class MergeConfig:
    merge_contacts: bool = True
    merge_domains: bool = True
    merge_names: bool = True


@dataclass
class MergeResult:
    primary_id: uuid.UUID
    duplicate_id: uuid.UUID
    deleted: bool                 # False when the duplicate row is kept (NetSuite)
    use_instead_set: bool
    counts: dict = field(default_factory=dict)   # per-table reassignment counts
    warnings: list = field(default_factory=list)


def _supplier_domains(sup: Supplier) -> set[str]:
    """Registrable domains for a supplier: url, alt_domains, contact urls/emails."""
    domains: set[str] = set()
    for raw in [sup.url, *(sup.alt_domains or [])]:
        if raw and "//" not in raw:  # bare domain stored without scheme
            raw = f"http://{raw}"
        d = _extract_domain(raw)
        if d:
            domains.add(d)
    for c in (sup.contacts or []):
        if not isinstance(c, dict):
            continue
        raw = c.get("url")
        if raw and "//" not in raw:
            raw = f"http://{raw}"
        d = _extract_domain(raw)
        if d:
            domains.add(d)
        email = c.get("email") or ""
        if "@" in email:
            d = _extract_domain(f"http://{email.rsplit('@', 1)[-1]}")
            if d:
                domains.add(d)
    return domains


def _merge_names(primary: Supplier, duplicate: Supplier) -> None:
    alt_names = list(primary.alt_names or [])
    existing = {n.lower() for n in alt_names}
    existing.add((primary.name or "").lower())
    for name in [duplicate.name, *(duplicate.alt_names or [])]:
        if name and name.lower() not in existing:
            alt_names.append(name)
            existing.add(name.lower())
    primary.alt_names = alt_names
    flag_modified(primary, "alt_names")


def _merge_domains(primary: Supplier, duplicate: Supplier) -> None:
    alt_domains = list(primary.alt_domains or [])
    known = {d.lower() for d in alt_domains}
    if primary.url:
        primary_domain = _extract_domain(primary.url)
        if primary_domain:
            known.add(primary_domain.lower())
    for d in _supplier_domains(duplicate):
        if d.lower() not in known:
            alt_domains.append(d)
            known.add(d.lower())
    primary.alt_domains = alt_domains
    flag_modified(primary, "alt_domains")
    if not primary.url and duplicate.url:
        primary.url = duplicate.url


def _merge_contacts(primary: Supplier, duplicate: Supplier) -> None:
    merged = list(primary.contacts or [])
    keys = {
        f"{c.get('url', '')}|{c.get('email', '')}"
        for c in merged if isinstance(c, dict)
    }
    for c in (duplicate.contacts or []):
        if not isinstance(c, dict):
            continue
        key = f"{c.get('url', '')}|{c.get('email', '')}"
        if key not in keys:
            merged.append(c)
            keys.add(key)
    primary.contacts = merged
    flag_modified(primary, "contacts")


def _reassign_rfq_items(session, primary: Supplier, duplicate: Supplier, counts: dict) -> None:
    """Rewrite supplier_id references inside rfq_items JSONB columns."""
    primary_id, duplicate_id = str(primary.id), str(duplicate.id)

    rfq_items = 0
    brand_supplier_items = 0
    for item in session.query(RFQItem).filter(RFQItem.suppliers.isnot(None)):
        changed = False
        for entry in item.suppliers or []:
            if isinstance(entry, dict) and entry.get("supplier_id") == duplicate_id:
                entry["supplier_id"] = primary_id
                entry["name"] = primary.name
                changed = True
        if changed:
            flag_modified(item, "suppliers")
            rfq_items += 1

    for item in session.query(RFQItem).filter(RFQItem.brand_suppliers.isnot(None)):
        changed = False
        for entry in item.brand_suppliers or []:
            if isinstance(entry, dict) and entry.get("supplier_id") == duplicate_id:
                entry["supplier_id"] = primary_id
                entry["name"] = primary.name
                changed = True
        if changed:
            flag_modified(item, "brand_suppliers")
            brand_supplier_items += 1

    counts["rfq_items"] = rfq_items
    counts["rfq_items_brand_suppliers"] = brand_supplier_items


def _reassign_rfq_supplier_meta(session, primary: Supplier, duplicate: Supplier, counts: dict) -> None:
    """Remap RFQ.supplier_meta keys (keyed by supplier name) to the primary."""
    remapped = 0
    dup_names = {n.lower() for n in [duplicate.name, *(duplicate.alt_names or [])] if n}
    for rfq in session.query(RFQ).filter(RFQ.supplier_meta.isnot(None)):
        meta = rfq.supplier_meta or {}
        new_meta = {}
        changed = False
        for key, value in meta.items():
            if key.lower() in dup_names and key.lower() != (primary.name or "").lower():
                new_meta[primary.name] = value
                changed = True
            else:
                new_meta[key] = value
        if changed:
            rfq.supplier_meta = new_meta
            flag_modified(rfq, "supplier_meta")
            remapped += 1
    counts["rfq_supplier_meta"] = remapped


def _reassign_fks(session, primary: Supplier, duplicate: Supplier, counts: dict) -> None:
    """Bulk-reassign every FK-based reference from duplicate to primary."""
    primary_id, duplicate_id = primary.id, duplicate.id

    # supplier_brands: delete conflicts (pair already exists on primary),
    # reassign the rest.
    existing = {
        r[0] for r in session.query(SupplierBrand.brand_id)
        .filter(SupplierBrand.supplier_id == primary_id).all()
    }
    brand_links = session.query(SupplierBrand).filter(
        SupplierBrand.supplier_id == duplicate_id
    ).all()
    moved, dropped = 0, 0
    for link in brand_links:
        if link.brand_id in existing:
            session.delete(link)
            dropped += 1
        else:
            link.supplier_id = primary_id
            moved += 1
    counts["supplier_brands_moved"] = moved
    counts["supplier_brands_dropped"] = dropped

    for model, key in ((EmailTracking, "email_tracking"),
                       (Contact, "contacts"),
                       (Transaction, "transactions")):
        n = (
            session.query(model)
            .filter(model.supplier_id == duplicate_id)
            .update({"supplier_id": primary_id}, synchronize_session=False)
        )
        counts[key] = n


def merge_suppliers(
    session,
    primary_id,
    duplicate_id,
    config: MergeConfig | None = None,
) -> MergeResult:
    """Merge duplicate supplier into primary, reassigning every reference.

    Raises ValueError for invalid merges (missing row, wrong web/netsuite
    direction, already merged). Never commits.
    """
    config = config or MergeConfig()

    primary_uuid = uuid.UUID(str(primary_id))
    duplicate_uuid = uuid.UUID(str(duplicate_id))
    if primary_uuid == duplicate_uuid:
        raise ValueError("primary and duplicate must be different suppliers")

    primary = session.query(Supplier).filter(Supplier.id == primary_uuid).first()
    duplicate = session.query(Supplier).filter(Supplier.id == duplicate_uuid).first()
    if not primary or not duplicate:
        raise ValueError("Supplier not found.")
    if duplicate.use_instead == primary.id:
        raise ValueError("Duplicate already merged into this primary.")

    # Web vs NetSuite matrix
    if not primary.netsuite_id and duplicate.netsuite_id:
        raise ValueError(
            "Cannot merge a NetSuite supplier into a web supplier — swap primary/duplicate."
        )
    keep_duplicate_row = bool(primary.netsuite_id and duplicate.netsuite_id)
    warnings = []
    if keep_duplicate_row:
        warnings.append(
            "Duplicate kept (netsuite_id present) — use_instead set on the record."
        )

    counts: dict = {}

    # 1. Field merges per config
    if config.merge_names:
        _merge_names(primary, duplicate)
    if config.merge_domains:
        _merge_domains(primary, duplicate)
    if config.merge_contacts:
        _merge_contacts(primary, duplicate)

    # 2. Reference reassignment (always)
    _reassign_rfq_items(session, primary, duplicate, counts)
    _reassign_rfq_supplier_meta(session, primary, duplicate, counts)
    _reassign_fks(session, primary, duplicate, counts)

    # 3. Keep or delete the duplicate row
    deleted = False
    use_instead_set = False
    if keep_duplicate_row:
        duplicate.use_instead = primary.id
        use_instead_set = True
        rebuild_match_keys(session, duplicate)
    else:
        session.delete(duplicate)
        deleted = True

    rebuild_match_keys(session, primary)

    return MergeResult(
        primary_id=primary.id,
        duplicate_id=duplicate.id,
        deleted=deleted,
        use_instead_set=use_instead_set,
        counts=counts,
        warnings=warnings,
    )


def pick_keep_remove(
    a: Supplier,
    b: Supplier,
    stats: dict | None = None,
) -> tuple[Supplier, Supplier]:
    """Pick which supplier to keep (primary) and which to remove (duplicate).

    Scoring (netsuite_id is absolute — a NetSuite record always beats a web
    one, since the merge matrix rejects web-primary/NetSuite-duplicate;
    transactions and recency decide between otherwise-similar sides):
        netsuite_id        +200 (dominates; max activity bonus is 140)
        transaction count  +2 each, capped at 50 (+100 max)
        last txn ≤ 90d     +40
        last txn ≤ 1y      +20
        last txn ≤ 2y      +10
        url                +10
        each contact       +2
        each alt_name      +1
        each alt_domain    +1

    stats: optional {supplier_id: (txn_count, latest_txn_date)} from the
    transactions table; suppliers without an entry score 0 for activity.
    """
    def _score(sup: Supplier) -> int:
        s = 0
        if sup.netsuite_id:
            s += 200
        if sup.url:
            s += 10
        s += len(sup.contacts or []) * 2
        s += len(sup.alt_names or [])
        s += len(sup.alt_domains or [])

        count, latest = (stats or {}).get(sup.id, (0, None))
        s += min(count or 0, 50) * 2
        if latest is not None:
            latest_date = latest.date() if hasattr(latest, "date") else latest
            days = (datetime.now(timezone.utc).date() - latest_date).days
            if days <= 90:
                s += 40
            elif days <= 365:
                s += 20
            elif days <= 730:
                s += 10
        return s

    if _score(a) >= _score(b):
        return a, b
    return b, a
