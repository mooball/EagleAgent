"""
sync_netsuite_suppliers.py

Syncs vendor records from NetSuite into the local suppliers table.
Uses the NetSuite REST API with SuiteQL queries.

For each vendor:
  - If it exists locally by netsuite_id: update changed fields
  - If new: insert a new supplier record
  - EagleAgent-only fields (embeddings, categories, comments) are never overwritten

Usage:
  uv run python -m scripts.sync_netsuite_suppliers
  uv run python -m scripts.sync_netsuite_suppliers --since 2026-04-01
  uv run python -m scripts.sync_netsuite_suppliers --since "7 days"
  uv run python -m scripts.sync_netsuite_suppliers --resume
  uv run python -m scripts.sync_netsuite_suppliers --dry-run
"""

import argparse
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import Brand, Contact, Supplier, SupplierBrand
from includes.dashboard.supplier_matching import rebuild_match_keys
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import suppliers_updated_since
from includes.netsuite.sync_utils import normalize_currency


def parse_since(value: str) -> str:
    """Parse a --since value into an ISO date string (YYYY-MM-DD).

    Accepts either:
      - An ISO date: '2026-04-01'
      - A relative period: '7 days', '7days', '7d'
    """
    match = re.match(r"^(\d+)\s*d(?:ays?)?$", value.strip(), re.IGNORECASE)
    if match:
        days = int(match.group(1))
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return value


def get_engine():
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def parse_netsuite_date(date_str: str | None) -> datetime | None:
    """Parse NetSuite date format (d/m/yyyy) to a datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def build_contacts(row: dict) -> list[dict]:
    """Build contact entries from NetSuite vendor fields for the contacts table."""
    contacts = []

    # Main contact (email + phone from vendor record)
    email = row.get("email")
    phone = row.get("phone")
    if email or phone:
        contacts.append({
            "label": "Main",
            "fullname": None,
            "email": email,
            "phone": phone,
        })

    # Source contact (go_source fields — used for ordering/sourcing)
    source_email = row.get("custentity_go_souce_email_address")
    source_name = row.get("custentity_go_souce_email_name")
    source_cc = row.get("custentity_go_souce_cc_email_addresses")

    # Skip if source_name is "undefined" (NetSuite data quality issue)
    if source_name and source_name.lower() == "undefined":
        source_name = None

    if source_email or source_name:
        contacts.append({
            "label": "Source",
            "fullname": source_name,
            "email": source_email,
            "phone": None,
        })

    if source_cc:
        contacts.append({
            "label": "Source CC",
            "fullname": None,
            "email": source_cc,
            "phone": None,
        })

    return contacts


def map_vendor_to_supplier(row: dict) -> dict:
    """Map a NetSuite vendor row to supplier column values."""
    return {
        "netsuite_id": str(row.get("id", "")).strip(),
        "name": (row.get("companyname") or row.get("entityid") or "").strip(),
        "url": row.get("url"),
        "address_1": row.get("addr1"),
        "address_2": row.get("addr2"),
        "city": row.get("city"),
        "state": row.get("state"),
        "postcode": row.get("zip"),
        "country": row.get("country"),
        "notes": row.get("custentity_supplier_notes"),
        "terms": row.get("terms"),
        "currency": normalize_currency(row.get("currency")),
        "hubspot_id": row.get("custentity_ss_hubspot_id"),
        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
    }


# Fields that NetSuite owns — these get overwritten on sync.
# Deliberately excludes use_instead (merged-duplicate flag) and
# alt_names/alt_domains (merged data) — a sync must never clobber them.
NETSUITE_OWNED_FIELDS = {
    "name", "url", "address_1", "address_2", "city", "state",
    "postcode", "country", "notes", "terms", "currency", "hubspot_id",
    "netsuite_last_modified",
}


def parse_brand_ids(row: dict) -> list[str]:
    """Parse comma-separated brand IDs from the custentity_supplier_brand field."""
    raw = row.get("custentity_supplier_brand")
    if not raw or not isinstance(raw, str):
        return []
    return [bid.strip() for bid in raw.split(",") if bid.strip()]


def sync_supplier_brands(session, supplier, brand_ids: list[str], brand_cache: dict):
    """Replace all SupplierBrand links for a supplier with the given brand IDs."""
    # Delete existing links
    session.query(SupplierBrand).filter(SupplierBrand.supplier_id == supplier.id).delete()

    # Insert new links
    linked = 0
    for brand_netsuite_id in brand_ids:
        brand = brand_cache.get(brand_netsuite_id)
        if brand:
            session.add(SupplierBrand(supplier_id=supplier.id, brand_id=brand.id))
            linked += 1

    return linked


def sync_supplier_contacts(session, supplier, contact_entries: list[dict]):
    """Upsert supplier contacts by label (Main, Source, Source CC).

    For supplier contacts there is no netsuite_id — matching is by
    supplier_id + label.  Contacts not present in the new list are
    marked inactive.
    """
    existing = (
        session.query(Contact)
        .filter(Contact.supplier_id == supplier.id)
        .all()
    )
    existing_by_label = {c.label: c for c in existing if c.label}

    seen_labels = set()
    for entry in contact_entries:
        label = entry.get("label")
        if not label:
            continue
        seen_labels.add(label)

        if label in existing_by_label:
            c = existing_by_label[label]
            c.fullname = entry.get("fullname")
            c.email = entry.get("email")
            c.phone = entry.get("phone")
            c.isinactive = False
        else:
            session.add(Contact(
                supplier_id=supplier.id,
                label=label,
                fullname=entry.get("fullname"),
                email=entry.get("email"),
                phone=entry.get("phone"),
            ))

    # Mark contacts with labels no longer present as inactive
    for label, c in existing_by_label.items():
        if label not in seen_labels:
            c.isinactive = True


def main():
    parser = argparse.ArgumentParser(description="Sync suppliers from NetSuite API into the database.")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only sync vendors modified since this date (YYYY-MM-DD or 'N days'). Default: 30 days.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified from DB).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display changes without writing to DB.")
    args = parser.parse_args()

    # Determine the since_date
    engine = get_engine()
    ResumeSession = sessionmaker(bind=engine)

    if args.resume:
        with ResumeSession() as session:
            from sqlalchemy import func
            max_date = session.query(func.max(Supplier.netsuite_last_modified)).filter(
                Supplier.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since_date = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since_date}")
        else:
            since_date = "2014-01-01"
            print("No existing synced suppliers found. Starting full sync from 2014-01-01.")
    elif args.since:
        since_date = parse_since(args.since)
    else:
        since_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE

    # Connect to NetSuite
    print("Connecting to NetSuite...")
    client = NetSuiteClient()
    query = suppliers_updated_since(since_date)
    print(f"Fetching vendors modified since {since_date}...")

    if args.dry_run:
        # For dry-run, collect a sample and exit
        rows = client.suiteql(query)
        rows = [{k: v for k, v in row.items() if k != "links"} for row in rows]
        print(f"Fetched {len(rows)} vendor records.\n")
        print("DRY RUN — first 10 vendors:")
        for row in rows[:10]:
            mapped = map_vendor_to_supplier(row)
            print(f"  [{mapped['netsuite_id']}] {mapped['name']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    # Connect to database and sync using streaming pagination + batch commits
    print("Connecting to database...")
    Session = sessionmaker(bind=engine)

    inserted = 0
    updated = 0
    skipped = 0
    brands_linked = 0
    processed = 0
    fetched = 0

    with Session() as session:
        # Build brand cache: netsuite_id -> Brand object
        brand_cache = {b.netsuite_id: b for b in session.query(Brand).all() if b.netsuite_id}

        # Pre-load web-sourced suppliers for dedup during inserts
        from includes.dashboard.database import _extract_domain
        _web_suppliers = (
            session.query(Supplier)
            .filter(Supplier.source == "web", Supplier.netsuite_id.is_(None))
            .all()
        )
        # Index by lowercase name and domain for fast lookup
        _web_by_name: dict[str, "Supplier"] = {}
        _web_by_domain: dict[str, "Supplier"] = {}
        for ws in _web_suppliers:
            if ws.name:
                _web_by_name[ws.name.strip().lower()] = ws
            ws_domain = _extract_domain(ws.url)
            if ws_domain:
                _web_by_domain[ws_domain] = ws

        for page in client.suiteql_iter(query):
            # Strip links metadata from each row in the page
            page = [{k: v for k, v in row.items() if k != "links"} for row in page]
            fetched += len(page)

            for row in page:
                mapped = map_vendor_to_supplier(row)
                netsuite_id = mapped["netsuite_id"]

                if not netsuite_id:
                    skipped += 1
                    processed += 1
                    continue

                # Look up existing supplier
                existing = session.query(Supplier).filter(Supplier.netsuite_id == netsuite_id).first()

                if existing:
                    # Update only NetSuite-owned fields that have changed
                    changed = False
                    for field in NETSUITE_OWNED_FIELDS:
                        new_value = mapped[field]
                        old_value = getattr(existing, field)

                        if new_value != old_value:
                            setattr(existing, field, new_value)
                            changed = True

                    if changed:
                        existing.modified_by = "netsuite"
                        updated += 1
                    else:
                        skipped += 1

                    # Sync brand links
                    brand_ids = parse_brand_ids(row)
                    brands_linked += sync_supplier_brands(session, existing, brand_ids, brand_cache)

                    # Sync contacts to contacts table
                    sync_supplier_contacts(session, existing, build_contacts(row))

                    if changed:
                        rebuild_match_keys(session, existing)
                else:
                    # Check for existing web-sourced supplier that matches by domain or exact name
                    merged_web = None
                    incoming_domain = _extract_domain(mapped.get("url"))
                    if incoming_domain and incoming_domain in _web_by_domain:
                        merged_web = _web_by_domain[incoming_domain]
                    elif mapped["name"] and mapped["name"].strip().lower() in _web_by_name:
                        merged_web = _web_by_name[mapped["name"].strip().lower()]

                    if merged_web:
                        # Merge: upgrade web record with NetSuite data
                        merged_web.netsuite_id = netsuite_id
                        merged_web.source = "netsuite"
                        for field in NETSUITE_OWNED_FIELDS:
                            new_value = mapped[field]
                            if new_value is not None:
                                setattr(merged_web, field, new_value)
                        merged_web.modified_by = "netsuite"
                        updated += 1
                        print(f"  Merged web supplier '{merged_web.name}' with NetSuite ID {netsuite_id}")

                        brand_ids = parse_brand_ids(row)
                        brands_linked += sync_supplier_brands(session, merged_web, brand_ids, brand_cache)

                        # Sync contacts to contacts table
                        sync_supplier_contacts(session, merged_web, build_contacts(row))

                        rebuild_match_keys(session, merged_web)
                    else:
                        # Insert new supplier
                        supplier = Supplier(
                            netsuite_id=netsuite_id,
                            name=mapped["name"],
                            url=mapped["url"],
                            address_1=mapped["address_1"],
                            address_2=mapped["address_2"],
                            city=mapped["city"],
                            state=mapped["state"],
                            postcode=mapped["postcode"],
                            country=mapped["country"],
                            notes=mapped["notes"],
                            terms=mapped["terms"],
                            hubspot_id=mapped["hubspot_id"],
                            netsuite_last_modified=mapped["netsuite_last_modified"],
                            modified_by="netsuite",
                            source="netsuite",
                        )
                        session.add(supplier)
                        session.flush()  # Get the supplier ID for brand/contact linking
                        inserted += 1

                        # Sync brand links for new supplier
                        brand_ids = parse_brand_ids(row)
                        brands_linked += sync_supplier_brands(session, supplier, brand_ids, brand_cache)

                        # Sync contacts to contacts table
                        sync_supplier_contacts(session, supplier, build_contacts(row))

                        rebuild_match_keys(session, supplier)

                processed += 1

                # Batch commit
                if processed % batch_size == 0:
                    session.commit()
                    print(f"  Committed batch ({processed} rows processed, {fetched} fetched)...")

        # Final commit for remaining rows
        session.commit()

    print(f"\nSync complete: {inserted} inserted, {updated} updated, {skipped} unchanged.")
    print(f"Brand links: {brands_linked} total links created.")

    # Summary
    with Session() as session:
        total = session.query(Supplier).count()
        print(f"Total suppliers in database: {total}")

    engine.dispose()


if __name__ == "__main__":
    main()
