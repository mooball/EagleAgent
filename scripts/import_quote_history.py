"""
import_quote_history.py

Imports quote history CSV data from the designated IMPORT_DIR into the
product_suppliers table. Matches product_netsuite_id → products.id and
supplier → suppliers.id using pre-built lookup caches.

Only rows where the 'sale' column is 'Expired' are imported.

Usage:
  Local database import:
    uv run python -m scripts.import_quote_history

  Production database import:
    uv run python -m scripts.import_quote_history --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import re
import glob
import numpy as np
import pandas as pd
import argparse
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.db_models import Product, Supplier, ProductSupplier

BATCH_SIZE = 200


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url, pool_size=2, max_overflow=0, pool_pre_ping=True, pool_recycle=120)


def make_session(engine):
    return sessionmaker(bind=engine)()


def clean_string(val):
    """Strip whitespace, collapse multiple spaces, return None for empty/NaN."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    val = str(val).strip()
    val = re.sub(r'\s+', ' ', val)
    return val if val and val.lower() not in ('nan', 'none') else None


def normalize_key(val: str) -> str:
    """Normalize a string for lookup matching: lowercase, collapse spaces."""
    return re.sub(r'\s+', ' ', val.strip()).lower()


def safe_float(val):
    """Convert to float safely, return None for missing/unparseable."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_date(val):
    """Parse date from d/m/yyyy or dd/mm/yyyy format."""
    s = clean_string(val)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


def build_product_netsuite_lookup(engine):
    """Build {netsuite_id: product_id} lookup from all products."""
    session = make_session(engine)
    try:
        products = session.query(Product.id, Product.netsuite_id).filter(Product.netsuite_id.isnot(None)).all()
        lookup = {}
        for pid, nid in products:
            if nid and nid not in lookup:
                lookup[nid] = pid
        print(f"  Product netsuite lookup: {len(lookup)} entries.")
        return lookup
    finally:
        session.close()


def build_supplier_lookup(engine):
    """Build {name_lower: supplier_id} lookup from all suppliers."""
    session = make_session(engine)
    try:
        suppliers = session.query(Supplier.id, Supplier.name).all()
        lookup = {}
        for sid, name in suppliers:
            if name:
                key = normalize_key(name)
                if key not in lookup:
                    lookup[key] = sid
        print(f"  Supplier lookup: {len(lookup)} unique supplier names.")
        return lookup
    finally:
        session.close()


def build_existing_keys(engine):
    """Load existing (doc_number, product_id, supplier_id, date) tuples for duplicate detection."""
    session = make_session(engine)
    try:
        rows = session.query(
            ProductSupplier.doc_number,
            ProductSupplier.product_id,
            ProductSupplier.supplier_id,
            ProductSupplier.date,
        ).all()
        keys = {(r.doc_number, str(r.product_id), str(r.supplier_id), r.date) for r in rows}
        print(f"  Existing records loaded: {len(keys)} for duplicate detection.")
        return keys
    finally:
        session.close()


def import_quote_history(engine, df: pd.DataFrame, product_netsuite: dict, supplier_lookup: dict, existing_keys: set):
    """Import quote history rows into the product_suppliers table."""
    inserted = 0
    skipped = 0
    duplicates = 0
    filtered_sale = 0
    unmatched_products = set()
    unmatched_suppliers = set()
    failed_rows = []

    total_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(df), BATCH_SIZE):
        batch_df = df.iloc[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        if batch_num % 50 == 1:
            print(f"  Batch {batch_num}/{total_batches}...")

        session = make_session(engine)
        try:
            batch_rows = []
            for _, row in batch_df.iterrows():
                # Only import rows where sale == 'Expired'
                sale_val = clean_string(row.get('sale'))
                if not sale_val or sale_val.lower() != 'expired':
                    filtered_sale += 1
                    skipped += 1
                    continue

                doc_number = clean_string(row.get('doc_number'))
                netsuite_id = clean_string(row.get('product_netsuite_id'))
                supplier_name = clean_string(row.get('supplier'))

                if not doc_number or not netsuite_id:
                    failed_rows.append({**row, '_reason': 'missing doc_number or netsuite_id'})
                    skipped += 1
                    continue

                # Resolve product_id via netsuite_id
                product_id = product_netsuite.get(netsuite_id)
                if not product_id:
                    unmatched_products.add(netsuite_id)
                    failed_rows.append({**row, '_reason': f'unmatched netsuite_id {netsuite_id}'})
                    skipped += 1
                    continue

                # Resolve supplier_id
                if supplier_name:
                    supplier_id = supplier_lookup.get(normalize_key(supplier_name))
                    if not supplier_id:
                        unmatched_suppliers.add(supplier_name)
                        failed_rows.append({**row, '_reason': 'unmatched supplier'})
                        skipped += 1
                        continue
                else:
                    failed_rows.append({**row, '_reason': 'missing supplier'})
                    skipped += 1
                    continue

                row_date = parse_date(row.get('date'))
                dup_key = (doc_number, str(product_id), str(supplier_id), row_date)
                if dup_key in existing_keys:
                    duplicates += 1
                    skipped += 1
                    continue

                batch_rows.append({
                    'doc_number': doc_number,
                    'date': row_date,
                    'product_id': product_id,
                    'supplier_id': supplier_id,
                    'quantity': safe_float(row.get('quantity')),
                    'price': safe_float(row.get('price')),
                    'status': clean_string(row.get('status')),
                })
                existing_keys.add(dup_key)

            if not batch_rows:
                session.close()
                continue

            for record in batch_rows:
                new_rec = ProductSupplier(**record)
                session.add(new_rec)
                inserted += 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Print summary
    print(f"\n  Summary: {inserted} inserted, {skipped} skipped ({duplicates} duplicates, {filtered_sale} filtered by sale != 'Expired').")

    if unmatched_products:
        sorted_products = sorted(unmatched_products)
        print(f"\n  Unmatched netsuite IDs ({len(sorted_products)}):")
        for nid in sorted_products[:50]:
            print(f"    - {nid}")
        if len(sorted_products) > 50:
            print(f"    ... and {len(sorted_products) - 50} more.")

    if unmatched_suppliers:
        sorted_suppliers = sorted(unmatched_suppliers)
        print(f"\n  Unmatched supplier names ({len(sorted_suppliers)}):")
        for name in sorted_suppliers[:50]:
            print(f"    - {name}")
        if len(sorted_suppliers) > 50:
            print(f"    ... and {len(sorted_suppliers) - 50} more.")

    return inserted, skipped, failed_rows


def main():
    parser = argparse.ArgumentParser(description="Import quote history data into the database.")
    parser.add_argument("--production", action="store_true", help="Import data into the PRODUCTION database.")
    args = parser.parse_args()

    import_dir = Config.IMPORT_DIR
    if not os.path.exists(import_dir):
        print(f"Import directory '{import_dir}' does not exist. Creating it.")
        os.makedirs(import_dir, exist_ok=True)
        return

    csv_files = glob.glob(os.path.join(import_dir, "quote_history_import*.csv"))
    if not csv_files:
        print(f"No files matching 'quote_history_import*.csv' found in {import_dir}.")
        return

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)

    print("Building lookup caches...")
    product_netsuite = build_product_netsuite_lookup(engine)
    supplier_lookup = build_supplier_lookup(engine)
    existing_keys = build_existing_keys(engine)

    for filepath in csv_files:
        filename = os.path.basename(filepath)
        print(f"\nProcessing file: {filename}")

        df = pd.read_csv(
            filepath,
            quotechar='"',
            skipinitialspace=True,
            dtype={'doc_number': str, 'product_netsuite_id': str, 'part_number': str},
        )
        df = df.replace({np.nan: None})

        if 'doc_number' not in df.columns or 'product_netsuite_id' not in df.columns:
            print("Error: CSV must have 'doc_number' and 'product_netsuite_id' columns.")
            continue

        print(f"Found {len(df)} rows.")
        inserted, skipped, failed_rows = import_quote_history(engine, df, product_netsuite, supplier_lookup, existing_keys)

        # Export failed rows to CSV for investigation
        if failed_rows:
            failed_path = os.path.join(import_dir, "quote_history_failed.csv")
            failed_df = pd.DataFrame(failed_rows)
            failed_df.to_csv(failed_path, index=False)
            print(f"\n  Exported {len(failed_rows)} failed rows to {failed_path}")

        print(f"Finished processing {filename}.")

    engine.dispose()
    print("\nDone.")


if __name__ == "__main__":
    main()
