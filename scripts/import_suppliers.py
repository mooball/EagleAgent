"""
import_suppliers.py

Imports supplier CSV data from the designated IMPORT_DIR into the database.
Split into two phases for stability over remote/production connections:

  Phase 1 – Upsert supplier core fields (name, contacts, address, etc.)
  Phase 2 – Link suppliers to canonical brands via the supplier_brands join table

A local JSON cache (<import_dir>/.supplier_cache.json) stores netsuite_id → supplier UUID
and brand_name → brand_id mappings to minimise database round-trips.

Usage:
  Local database import:
    uv run python -m scripts.import_suppliers

  Production database import:
    uv run python -m scripts.import_suppliers --production
    (Requires PROD_DATABASE_URL to be set in your .env file)

  Run only phase 1 (suppliers) or phase 2 (brand links):
    uv run python -m scripts.import_suppliers --production --phase 1
    uv run python -m scripts.import_suppliers --production --phase 2
"""

import os
import re
import glob
import json
import logging
import numpy as np
import pandas as pd
import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.db_models import Supplier, Brand, SupplierBrand

logger = logging.getLogger(__name__)

CACHE_FILENAME = ".supplier_cache.json"
BATCH_SIZE = 50  # small batches to limit connection time


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(
        db_url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=120,
    )


def make_session(engine):
    """Create a new short-lived session."""
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# Local cache helpers
# ---------------------------------------------------------------------------

def cache_path(import_dir: str) -> str:
    return os.path.join(import_dir, CACHE_FILENAME)


def load_cache(import_dir: str, env_label: str) -> dict:
    """Load cache, invalidating if the target environment changed."""
    path = cache_path(import_dir)
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        if data.get("env") == env_label:
            return data
        print(f"  Cache was for '{data.get('env')}' but target is '{env_label}' — rebuilding.")
    return {"env": env_label, "suppliers": {}, "brands": {}}


def save_cache(import_dir: str, cache: dict):
    path = cache_path(import_dir)
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase 1 – upsert supplier core fields
# ---------------------------------------------------------------------------

def phase1_import_suppliers(engine, df: pd.DataFrame, cache: dict):
    """Upsert supplier rows (no brand linking). Updates the cache in-place."""
    inserted = 0
    updated = 0
    skipped = 0

    supplier_cache = cache.setdefault("suppliers", {})

    total_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(df), BATCH_SIZE):
        batch_df = df.iloc[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Phase 1 – batch {batch_num}/{total_batches}...")

        # Pre-parse rows
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

        # Short-lived session per batch
        session = make_session(engine)
        try:
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
                    supplier_cache[nid] = str(existing.id)
                    updated += 1
                else:
                    clean_rec = {k: v for k, v in record.items() if v is not None}
                    new_supplier = Supplier(**clean_rec)
                    session.add(new_supplier)
                    session.flush()
                    supplier_cache[nid] = str(new_supplier.id)
                    inserted += 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    print(f"\n  Phase 1 summary: {inserted} inserted, {updated} updated, {skipped} skipped.")
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Phase 2 – link suppliers → brands
# ---------------------------------------------------------------------------

def build_brand_lookup(engine, cache: dict) -> dict:
    """
    Build a case-insensitive brand name → canonical Brand ID lookup.
    Stores results in the cache for subsequent runs.
    """
    session = make_session(engine)
    try:
        all_brands = session.query(Brand).all()
    finally:
        session.close()

    lookup = {}
    canonical_map = {b.id: b for b in all_brands if b.duplicate_of is None}

    brand_cache = cache.setdefault("brands", {})
    for brand in all_brands:
        canonical_id = brand.duplicate_of if brand.duplicate_of else brand.id
        if canonical_id in canonical_map:
            key = brand.name.lower()
            lookup[key] = str(canonical_id)
            brand_cache[key] = str(canonical_id)

    return lookup


def phase2_link_brands(engine, df: pd.DataFrame, cache: dict, skip_batches: int = 0):
    """Link suppliers to brands using the CSV 'brands' column and cached IDs."""
    supplier_cache = cache.get("suppliers", {})
    brand_lookup = cache.get("brands", {})

    # If the brand cache is empty, populate it now
    if not brand_lookup:
        print("  Brand cache is empty – loading from database...")
        brand_lookup = build_brand_lookup(engine, cache)

    # If the supplier cache is empty, populate from DB
    if not supplier_cache:
        print("  Supplier cache is empty – loading from database...")
        session = make_session(engine)
        try:
            for s in session.query(Supplier).all():
                supplier_cache[s.netsuite_id] = str(s.id)
        finally:
            session.close()
        cache["suppliers"] = supplier_cache

    print(f"  Using {len(supplier_cache)} cached supplier IDs and {len(brand_lookup)} brand mappings.")

    brand_links = 0
    unmatched_brands = set()
    skipped_suppliers = 0

    # Build list of (supplier_id, [brand_names]) from CSV
    link_rows = []
    for _, row in df.iterrows():
        nid = clean_string(row.get("netsuite_id"))
        brands_csv = clean_string(row.get("brands"))
        if not nid or not brands_csv:
            continue
        supplier_id = supplier_cache.get(nid)
        if not supplier_id:
            skipped_suppliers += 1
            continue
        brand_names = [b.strip() for b in brands_csv.split(",") if b.strip()]
        if brand_names:
            link_rows.append((supplier_id, brand_names))

    total_batches = (len(link_rows) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(link_rows), BATCH_SIZE):
        batch = link_rows[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        if batch_num <= skip_batches:
            continue

        print(f"  Phase 2 – batch {batch_num}/{total_batches}...")

        session = make_session(engine)
        try:
            for supplier_id, brand_names in batch:
                for brand_name in brand_names:
                    canonical_id = brand_lookup.get(brand_name.lower())
                    if canonical_id is None:
                        unmatched_brands.add(brand_name)
                        continue

                    stmt = insert(SupplierBrand).values(
                        supplier_id=supplier_id,
                        brand_id=canonical_id,
                    )
                    stmt = stmt.on_conflict_do_nothing(constraint="uq_supplier_brand")
                    session.execute(stmt)
                    brand_links += 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    print(f"\n  Phase 2 summary: {brand_links} brand links created.")
    if skipped_suppliers:
        print(f"  {skipped_suppliers} suppliers skipped (not in cache – run phase 1 first).")

    if unmatched_brands:
        sorted_unmatched = sorted(unmatched_brands)
        print(f"\n  Unmatched brands ({len(sorted_unmatched)}):")
        for name in sorted_unmatched:
            print(f"    - {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Import Supplier data into the database.")
    parser.add_argument("--production", action="store_true", help="Import data into the PRODUCTION database.")
    parser.add_argument("--phase", type=int, choices=[1, 2], default=None,
                        help="Run only phase 1 (suppliers) or phase 2 (brand links). Default: both.")
    parser.add_argument("--skip-batches", type=int, default=0,
                        help="Skip this many batches (resume from batch N+1). Useful for resuming interrupted phase 2 runs.")
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

    run_phase1 = args.phase in (None, 1)
    run_phase2 = args.phase in (None, 2)

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)

    # Load or create local cache (tagged by environment)
    cache = load_cache(import_dir, env_label)
    print(f"Cache loaded: {len(cache.get('suppliers', {}))} suppliers, {len(cache.get('brands', {}))} brands.")

    for filepath in csv_files:
        filename = os.path.basename(filepath)
        print(f"\nProcessing file: {filename}")

        df = pd.read_csv(
            filepath,
            quotechar='"',
            skipinitialspace=True,
            dtype=str,
        )
        df = df.replace({np.nan: None})

        if "netsuite_id" not in df.columns or "name" not in df.columns:
            print("Error: CSV must have 'netsuite_id' and 'name' columns.")
            continue

        print(f"Found {len(df)} rows.")

        if run_phase1:
            print("\n--- Phase 1: Upsert suppliers ---")
            phase1_import_suppliers(engine, df, cache)
            save_cache(import_dir, cache)
            print(f"  Cache saved ({len(cache['suppliers'])} suppliers).")

        if run_phase2:
            # Always refresh brand lookup from the target database
            print("\n  Refreshing brand lookup from database...")
            build_brand_lookup(engine, cache)
            save_cache(import_dir, cache)
            print("--- Phase 2: Link brands ---")
            phase2_link_brands(engine, df, cache, skip_batches=args.skip_batches)
            save_cache(import_dir, cache)

        print(f"\nFinished processing {filename}.")

    engine.dispose()
    print("\nDone.")


if __name__ == "__main__":
    main()
