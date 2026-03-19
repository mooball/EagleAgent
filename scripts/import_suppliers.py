"""
import_suppliers.py

Imports supplier CSV data from the designated IMPORT_DIR into the database.
Handles upserts on netsuite_id, builds contacts JSON from CSV contact columns,
and links suppliers to canonical brands via the supplier_brands join table.

Usage:
  Local database import:
    uv run python -m scripts.import_suppliers

  Production database import:
    uv run python -m scripts.import_suppliers --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import re
import glob
import json
import logging
import numpy as np
import pandas as pd
import argparse

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.db_models import Supplier, Brand, SupplierBrand

logger = logging.getLogger(__name__)


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def clean_string(val):
    """Strip whitespace and collapse multiple spaces. Returns None for empty values."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    val = str(val).strip()
    val = re.sub(r"\s+", " ", val)
    return val if val else None


def build_contacts(row) -> list:
    """Build a contacts JSON array from CSV contact columns."""
    contacts = []

    main_email = clean_string(row.get("email"))
    main_phone = clean_string(row.get("phone"))
    if main_email or main_phone:
        contacts.append({
            "label": "Main",
            "name": None,
            "email": main_email,
            "phone": main_phone,
        })

    best_name = clean_string(row.get("best_contact_name"))
    best_email = clean_string(row.get("best_contact_email"))
    if best_name or best_email:
        contacts.append({
            "label": "Best",
            "name": best_name,
            "email": best_email,
            "phone": None,
        })

    return contacts if contacts else None


def build_brand_lookup(session) -> dict:
    """
    Build a case-insensitive brand name → canonical Brand ID lookup.
    Includes both canonical and duplicate brand names, all resolving to the canonical ID.
    """
    all_brands = session.query(Brand).all()
    lookup = {}
    canonical_map = {b.id: b for b in all_brands if b.duplicate_of is None}

    for brand in all_brands:
        canonical_id = brand.duplicate_of if brand.duplicate_of else brand.id
        # Only map to canonical brands that exist
        if canonical_id in canonical_map:
            lookup[brand.name.lower()] = canonical_id

    return lookup


def link_supplier_brands(session, supplier_id, brands_csv: str, brand_lookup: dict, unmatched_brands: set):
    """Parse comma-separated brands and link to supplier via join table."""
    if not brands_csv:
        return 0

    linked = 0
    brand_names = [b.strip() for b in brands_csv.split(",") if b.strip()]

    for brand_name in brand_names:
        canonical_id = brand_lookup.get(brand_name.lower())
        if canonical_id is None:
            unmatched_brands.add(brand_name)
            continue

        # Insert if not already linked
        stmt = insert(SupplierBrand).values(
            supplier_id=supplier_id,
            brand_id=canonical_id,
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_supplier_brand")
        session.execute(stmt)
        linked += 1

    return linked


def import_suppliers(session, df: pd.DataFrame, brand_lookup: dict):
    """Import suppliers from a cleaned DataFrame."""
    inserted = 0
    updated = 0
    skipped = 0
    brand_links = 0
    unmatched_brands = set()

    batch_size = 200
    for i in range(0, len(df), batch_size):
        batch_df = df.iloc[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(df) + batch_size - 1) // batch_size
        print(f"  Batch {batch_num}/{total_batches}...")

        # Pre-parse all rows in this batch
        batch_rows = []
        for _, row in batch_df.iterrows():
            nid = clean_string(row.get("netsuite_id"))
            name = clean_string(row.get("name"))
            if not nid or not name:
                skipped += 1
                continue
            batch_rows.append((nid, name, row))

        if not batch_rows:
            continue

        # Pre-fetch all existing suppliers for this batch in ONE query
        batch_nids = [nid for nid, _, _ in batch_rows]
        existing_suppliers = session.query(Supplier).filter(Supplier.netsuite_id.in_(batch_nids)).all()
        existing_map = {s.netsuite_id: s for s in existing_suppliers}

        for nid, name, row in batch_rows:
            contacts = build_contacts(row)

            record = {
                "netsuite_id": nid,
                "name": name,
                "url": clean_string(row.get("url")),
                "address_1": clean_string(row.get("address_1")),
                "city": clean_string(row.get("city")),
                "country": clean_string(row.get("country")),
                "notes": clean_string(row.get("notes")),
                "contacts": contacts,
            }

            existing = existing_map.get(nid)
            if existing:
                for k, v in record.items():
                    if v is not None:
                        setattr(existing, k, v)
                supplier_id = existing.id
                updated += 1
            else:
                clean_rec = {k: v for k, v in record.items() if v is not None}
                new_supplier = Supplier(**clean_rec)
                session.add(new_supplier)
                session.flush()
                supplier_id = new_supplier.id
                inserted += 1

            # Link brands
            brands_csv = clean_string(row.get("brands"))
            brand_links += link_supplier_brands(session, supplier_id, brands_csv, brand_lookup, unmatched_brands)

        session.commit()

    print(f"\nSupplier summary: {inserted} inserted, {updated} updated, {skipped} skipped.")
    print(f"Brand links created: {brand_links}")

    if unmatched_brands:
        sorted_unmatched = sorted(unmatched_brands)
        print(f"\nUnmatched brands ({len(sorted_unmatched)}):")
        for name in sorted_unmatched:
            print(f"  - {name}")


def main():
    parser = argparse.ArgumentParser(description="Import Supplier data into the database.")
    parser.add_argument("--production", action="store_true", help="Import data into the PRODUCTION database.")
    args = parser.parse_args()

    import_dir = Config.IMPORT_DIR
    if not os.path.exists(import_dir):
        print(f"Import directory '{import_dir}' does not exist. Creating it.")
        os.makedirs(import_dir, exist_ok=True)
        return

    csv_files = glob.glob(os.path.join(import_dir, "suppliers_import*.csv"))
    if not csv_files:
        print(f"No files matching 'suppliers_import*.csv' found in {import_dir}.")
        return

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Build brand lookup once
        brand_lookup = build_brand_lookup(session)
        print(f"Loaded {len(brand_lookup)} brand name mappings.")

        for filepath in csv_files:
            filename = os.path.basename(filepath)
            print(f"\nProcessing file: {filename}")

            df = pd.read_csv(
                filepath,
                quotechar='"',
                skipinitialspace=True,
                dtype=str,  # Read all columns as strings to preserve data
            )

            # Replace pandas NaN with None
            df = df.replace({np.nan: None})

            if "netsuite_id" not in df.columns or "name" not in df.columns:
                print("Error: CSV must have 'netsuite_id' and 'name' columns.")
                continue

            print(f"Found {len(df)} rows.")
            import_suppliers(session, df, brand_lookup)
            print(f"Finished processing {filename}.")


if __name__ == "__main__":
    main()
