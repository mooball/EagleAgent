"""One-off script: update supplier terms and contacts from CSV. TOM 28 APR 2026

Usage:
    # Dry run (default) — shows what would change
    uv run python -m scripts.update_supplier_terms_contacts

    # Apply changes locally
    uv run python -m scripts.update_supplier_terms_contacts --apply

    # Dry run against production
    uv run python -m scripts.update_supplier_terms_contacts --production

    # Apply changes to production (requires PROD_DATABASE_URL in .env)
    uv run python -m scripts.update_supplier_terms_contacts --production --apply
"""

import csv
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

CSV_PATH = Path(__file__).parent.parent / "data" / "import" / "supplier_update-april-29.csv"


def _get_session(is_prod: bool):
    """Create a DB session, optionally targeting production."""
    if is_prod:
        from config.settings import Config
        db_url = Config.PROD_DATABASE_URL
        if not db_url:
            print("ERROR: PROD_DATABASE_URL not set in .env")
            sys.exit(1)
        if db_url.startswith("postgresql+asyncpg://"):
            db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        print(f"Connected to PRODUCTION database")
        return Session()
    else:
        from includes.dashboard.database import get_session
        return get_session()


def main():
    apply = "--apply" in sys.argv
    is_prod = "--production" in sys.argv

    from includes.dashboard.models import Supplier

    session = _get_session(is_prod)

    # Load CSV — note the header typo: "ncontact_email" instead of "contact_email"
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"CSV rows: {len(rows)}")

    # Build lookup of existing suppliers by netsuite_id
    suppliers = session.query(Supplier).filter(Supplier.netsuite_id.isnot(None)).all()
    by_netsuite_id = {s.netsuite_id: s for s in suppliers}
    print(f"DB suppliers with netsuite_id: {len(by_netsuite_id)}")

    matched = 0
    terms_updated = 0
    contacts_added = 0
    not_found = 0

    for row in rows:
        nid = row.get("netsuite_id", "").strip()
        if not nid:
            continue

        supplier = by_netsuite_id.get(nid)
        if not supplier:
            not_found += 1
            continue

        matched += 1
        changes = []

        # 1. Update terms
        terms = row.get("terms", "").strip()
        if terms and supplier.terms != terms:
            changes.append(f"terms: {supplier.terms!r} -> {terms!r}")
            if apply:
                supplier.terms = terms
            terms_updated += 1

        # 2. Add contact if email not already present
        email = row.get("ncontact_email", "").strip()  # CSV has typo in header
        name = row.get("contact_name", "").strip()
        if name == "undefined":
            name = ""

        if email:
            existing_contacts = supplier.contacts or []
            existing_emails = {
                (c.get("email") or "").lower()
                for c in existing_contacts
                if isinstance(c, dict)
            }

            if email.lower() not in existing_emails:
                new_contact = {
                    "name": name or None,
                    "email": email,
                    "label": "Main",
                    "phone": None,
                }
                changes.append(f"contact added: {email}" + (f" ({name})" if name else ""))
                if apply:
                    updated = list(existing_contacts) + [new_contact]
                    supplier.contacts = updated
                contacts_added += 1

        if changes:
            print(f"  [{nid}] {supplier.name}: {'; '.join(changes)}")

    print(f"\n--- Summary ---")
    print(f"Matched:         {matched}")
    print(f"Not found in DB: {not_found}")
    print(f"Terms updated:   {terms_updated}")
    print(f"Contacts added:  {contacts_added}")

    if apply:
        session.commit()
        print("\nChanges committed.")
    else:
        session.rollback()
        print("\nDry run — no changes written. Use --apply to commit.")

    session.close()


if __name__ == "__main__":
    main()
