"""
import_brands.py

Imports brand CSV data from the designated IMPORT_DIR into the database.
Handles upserts on netsuite_id and cleans brand names (whitespace, odd characters).

Usage:
  Local database import:
    uv run python -m scripts.import_brands

  Production database import:
    uv run python -m scripts.import_brands --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import re
import glob
import numpy as np
import pandas as pd
import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.db_models import Brand


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


# Regex whitelist: keep standard Latin letters (including accented), digits, and common punctuation
ALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9\s\-&.'\/,()#+À-ÖØ-öø-ÿ]")


def clean_brand_name(name: str) -> str:
    """Clean a brand name: strip, collapse whitespace, remove non-standard characters."""
    if not name or not isinstance(name, str):
        return name

    # Strip leading/trailing whitespace
    name = name.strip()

    # Remove non-standard characters
    name = ALLOWED_CHARS.sub("", name)

    # Collapse multiple spaces/tabs to a single space
    name = re.sub(r"\s+", " ", name)

    # Final strip in case removal left leading/trailing spaces
    name = name.strip()

    return name


def import_brands(session, df: pd.DataFrame):
    """Import brands from a cleaned DataFrame using upsert on netsuite_id."""
    inserted = 0
    updated = 0
    skipped = 0

    batch_size = 200
    for i in range(0, len(df), batch_size):
        batch_df = df.iloc[i : i + batch_size]
        print(f"  Batch {i // batch_size + 1}/{(len(df) + batch_size - 1) // batch_size}...")

        records = []
        for _, row in batch_df.iterrows():
            nid = str(row.get("netsuite_id")).strip() if pd.notna(row.get("netsuite_id")) else ""
            name = str(row.get("name")).strip() if pd.notna(row.get("name")) else ""

            if not nid:
                skipped += 1
                continue

            cleaned_name = clean_brand_name(name)
            if not cleaned_name:
                skipped += 1
                continue

            records.append({"netsuite_id": nid, "name": cleaned_name})

        if not records:
            continue

        # Check which netsuite_ids already exist in this batch
        netsuite_ids = [r["netsuite_id"] for r in records]
        existing = {
            b.netsuite_id
            for b in session.query(Brand.netsuite_id)
            .filter(Brand.netsuite_id.in_(netsuite_ids))
            .all()
        }

        for record in records:
            if record["netsuite_id"] in existing:
                updated += 1
            else:
                inserted += 1

        stmt = insert(Brand).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["netsuite_id"],
            set_={"name": stmt.excluded.name},
        )
        session.execute(stmt)
        session.commit()

    print(f"\nSummary: {inserted} inserted, {updated} updated, {skipped} skipped.")


def main():
    parser = argparse.ArgumentParser(description="Import Brand data into the database.")
    parser.add_argument("--production", action="store_true", help="Import data into the PRODUCTION database.")
    args = parser.parse_args()

    import_dir = Config.IMPORT_DIR
    if not os.path.exists(import_dir):
        print(f"Import directory '{import_dir}' does not exist. Creating it.")
        os.makedirs(import_dir, exist_ok=True)
        return

    csv_files = glob.glob(os.path.join(import_dir, "brands_import*.csv"))
    if not csv_files:
        print(f"No files matching 'brands_import*.csv' found in {import_dir}.")
        return

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        for filepath in csv_files:
            filename = os.path.basename(filepath)
            print(f"\nProcessing file: {filename}")

            df = pd.read_csv(
                filepath,
                quotechar='"',
                skipinitialspace=True,
                dtype={"netsuite_id": str, "name": str},
            )

            # Replace pandas NaN with None
            df = df.replace({np.nan: None})

            if "netsuite_id" not in df.columns or "name" not in df.columns:
                print("Error: CSV must have 'netsuite_id' and 'name' columns.")
                continue

            print(f"Found {len(df)} rows.")
            import_brands(session, df)
            print(f"Finished processing {filename}.")


if __name__ == "__main__":
    main()
