"""
import_products.py

Imports product and supplier CSV data from the designated IMPORT_DIR into the database.
Handles upserts dynamically to prevent overwriting existing valid data with blanks.

Usage:
  Local database import:
    uv run python -m scripts.import_products
    
  Production database import:
    uv run python -m scripts.import_products --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import glob
import numpy as np
import pandas as pd
import argparse
from typing import Dict, Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.db_models import Base, Product, Supplier

def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")
        
    # Normalize to use psycopg (v3) synchronous driver for the script
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
        
    return create_engine(db_url)

def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace and handle corrupt characters/NaNs safely."""
    # Strip whitespace from string columns
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).str.strip()
        # Replace empty strings or pandas 'nan' / 'None' with actual None
        df[col] = df[col].replace({'nan': None, 'None': None, '': None})
        
    # Replace Numpy/Float NaNs with None for SQLAlchemy compatibility
    df = df.replace({np.nan: None})
    return df

def import_products(session, df: pd.DataFrame):
    """Processes properly formatted Products CSV dataframe into the database."""
    print(f"Importing {len(df)} products...")
    
    # Process in batches to avoid PostgreSQL parameter limits (max 32767 for psycopg)
    batch_size = 200
    for i in range(0, len(df), batch_size):
        batch_df = df.iloc[i:i+batch_size]
        print(f"Merging batch {i // batch_size + 1}/{(len(df) + batch_size - 1) // batch_size}...")
        
        records = []
        for _, row in batch_df.iterrows():
            nid = str(row.get('netsuite_id')).strip() if pd.notna(row.get('netsuite_id')) else ""
            pn = str(row.get('part_number')).strip() if pd.notna(row.get('part_number')) else ""
            
            # Skip rows missing both primary identifiers
            if not nid and not pn:
                continue
                
            records.append({
                'netsuite_id': nid if nid else None,
                'part_number': pn if pn else None,
                'supplier_code': str(row.get('supplier_code')) if pd.notna(row.get('supplier_code')) else None,
                'description': str(row.get('description')) if pd.notna(row.get('description')) else None,
                'brand': str(row.get('brand')) if pd.notna(row.get('brand')) else None,
                'weight_kg': float(row.get('weight_kg')) if pd.notna(row.get('weight_kg')) else None,
                'length_m': float(row.get('length_m')) if pd.notna(row.get('length_m')) else None,
                'product_type': str(row.get('product_type')) if pd.notna(row.get('product_type')) else None
            })

        part_numbers = [r['part_number'] for r in records if r.get('part_number')]
        netsuite_ids = [r['netsuite_id'] for r in records if r.get('netsuite_id')]
        
        conditions = []
        if part_numbers:
            conditions.append(Product.part_number.in_(part_numbers))
        if netsuite_ids:
            conditions.append(Product.netsuite_id.in_(netsuite_ids))
            
        existing_map = {}
        if conditions:
            # Query existing records in this batch
            existing_products = session.query(Product).filter(or_(*conditions)).all()
            for p in existing_products:
                if p.part_number: existing_map[('part_number', p.part_number)] = p
                if p.netsuite_id: existing_map[('netsuite', p.netsuite_id)] = p

        for record in records:
            existing = None
            if record.get('netsuite_id'):
                existing = existing_map.get(('netsuite', record['netsuite_id']))
            if not existing and record.get('part_number'):
                existing = existing_map.get(('part_number', record['part_number']))
                
            if existing:
                # Update existing record ONLY with non-empty values
                for k, v in record.items():
                    if pd.notna(v) and v is not None and str(v).strip() != '':
                        setattr(existing, k, v)
            else:
                # Create new record
                clean_rec = {k: v for k, v in record.items() if pd.notna(v) and v is not None and str(v).strip() != ''}
                new_p = Product(**clean_rec)
                session.add(new_p)
                if new_p.part_number: existing_map[('part_number', new_p.part_number)] = new_p
                if new_p.netsuite_id: existing_map[('netsuite', new_p.netsuite_id)] = new_p
        
        session.commit()
        
    print("Database insert/update complete.")

def import_suppliers(session, df: pd.DataFrame):
    """Processes properly formatted Suppliers CSV dataframe into the database."""
    print(f"Importing {len(df)} suppliers...")
    
    records = []
    for _, row in df.iterrows():
        nid = str(row.get('netsuite_id')).strip() if pd.notna(row.get('netsuite_id')) else ""
        name = str(row.get('name')).strip() if pd.notna(row.get('name')) else ""
        
        if not nid or not name:
            continue
            
        records.append({
            'netsuite_id': nid,
            'name': name
        })
        
    stmt = insert(Supplier).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=['netsuite_id'], 
        set_={
            'name': stmt.excluded.name
        }
    )
    
    session.execute(stmt)
    session.commit()
    print("Database insert complete.")

def process_csv(filepath: str, session):
    """Reads a CSV, cleans it, and routes it to the right importer based on filename."""
    filename = os.path.basename(filepath).lower()
    
    print(f"\nProcessing file: {filename}")
    try:
        # Load CSV handling quoted fields properly
        # Enforce string dtype on identifiers to prevent pandas from dropping leading zeroes (e.g. '0034' -> 34.0)
        df = pd.read_csv(
            filepath, 
            quotechar='"', 
            skipinitialspace=True,
            dtype={'part_number': str, 'netsuite_id': str, 'supplier_code': str}
        )
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return
        
    # Basic Cleansing
    df = clean_dataframe(df)
    
    # Determine table
    if filename.startswith('products_import'):
        # Ensure we have a part_number for the products
        if 'part_number' not in df.columns:
            print("Error: `part_number` column is required in the CSV.")
            return
        import_products(session, df)
    elif filename.startswith('suppliers_import'):
        # Ensure we have required fields
        if 'name' not in df.columns:
            print("Error: `name` column is required in the CSV.")
            return
        import_suppliers(session, df)
    else:
        print(f"Unrecognized filename pattern '{filename}'. Skipping.")
        return
        
    print(f"Finished processing {filename}.")


def main():
    parser = argparse.ArgumentParser(description="Import Product and Supplier data into the database.")
    parser.add_argument("--production", action="store_true", help="Import data into the PRODUCTION database.")
    args = parser.parse_args()

    import_dir = Config.IMPORT_DIR
    if not os.path.exists(import_dir):
        print(f"Import directory '{import_dir}' does not exist. Creating it.")
        os.makedirs(import_dir, exist_ok=True)
        return
        
    # Get all .csv files matching *_import*.csv pattern
    csv_files = glob.glob(os.path.join(import_dir, "*_import*.csv"))
    if not csv_files:
        print(f"No files matching '*_import*.csv' found in {import_dir}.")
        return
        
    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        for filepath in csv_files:
            process_csv(filepath, session)

if __name__ == "__main__":
    main()
