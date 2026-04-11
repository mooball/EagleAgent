"""
sync_prod_data.py

Pulls production data for the core product tables into the local dev database.
Deletes local data from these tables first, then copies all rows from prod.

Tables synced (in dependency order):
  brands, suppliers, products, supplier_brands, product_suppliers

Usage:
    uv run python -m scripts.sync_prod_data

Requires PROD_DATABASE_URL and DATABASE_URL to be set in .env.
"""

import os
import sys
import time
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

# Tables in dependency order (children last)
TABLES = [
    "brands",
    "suppliers",
    "products",
    "supplier_brands",
    "product_suppliers",
]

# Reverse for deletion (children first)
DELETE_ORDER = list(reversed(TABLES))


def get_engine(url: str):
    """Create a sync SQLAlchemy engine, normalising the URL scheme to use psycopg (v3)."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    return create_engine(url)


def main():
    prod_url = os.getenv("PROD_DATABASE_URL")
    local_url = os.getenv("DATABASE_URL")

    if not prod_url:
        print("Error: PROD_DATABASE_URL is not set in .env")
        sys.exit(1)
    if not local_url:
        print("Error: DATABASE_URL is not set in .env")
        sys.exit(1)

    # Normalise local URL for sync driver
    prod_engine = get_engine(prod_url)
    local_engine = get_engine(local_url)

    ProdSession = sessionmaker(bind=prod_engine)
    LocalSession = sessionmaker(bind=local_engine)

    prod_session = ProdSession()
    local_session = LocalSession()

    try:
        # Step 1: Delete local data (children first)
        print("\n--- Clearing local tables ---")
        for table in DELETE_ORDER:
            count = local_session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            local_session.execute(text(f"DELETE FROM {table}"))
            print(f"  Deleted {count} rows from {table}")
        local_session.commit()

        # Step 2: Copy prod data (parents first)
        # Disable FK triggers so self-referencing tables (brands.duplicate_of) work
        print("\n--- Copying production data ---")
        total_start = time.monotonic()

        for table in TABLES:
            local_session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER ALL"))
        local_session.commit()

        for table in TABLES:
            t0 = time.monotonic()

            # Get column names and types from prod
            cols_result = prod_session.execute(
                text(
                    "SELECT column_name, data_type, udt_name FROM information_schema.columns "
                    "WHERE table_name = :table ORDER BY ordinal_position"
                ),
                {"table": table},
            )
            col_info = [(row[0], row[1], row[2]) for row in cols_result]
            columns = [c[0] for c in col_info]

            # Identify JSONB and vector columns that need special casting
            jsonb_cols = {c[0] for c in col_info if c[2] == "jsonb" or c[1] == "jsonb"}
            vector_cols = {c[0] for c in col_info if "vector" in c[2].lower() or c[1] == "USER-DEFINED"}

            col_list = ", ".join(columns)

            # Fetch all rows from prod
            rows = prod_session.execute(text(f"SELECT {col_list} FROM {table}")).fetchall()

            if not rows:
                print(f"  {table}: 0 rows (empty in prod)")
                continue

            # Build parameterised INSERT with type casts for special columns
            placeholders = []
            for c in columns:
                if c in jsonb_cols:
                    placeholders.append(f"CAST(:{c} AS jsonb)")
                elif c in vector_cols:
                    placeholders.append(f"CAST(:{c} AS vector)")
                else:
                    placeholders.append(f":{c}")
            insert_sql = text(
                f"INSERT INTO {table} ({col_list}) VALUES ({', '.join(placeholders)})"
            )

            # Insert in batches, serialising JSONB values to strings
            batch_size = 500
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                row_dicts = []
                for row in batch:
                    d = dict(zip(columns, row))
                    for jc in jsonb_cols:
                        if d[jc] is not None:
                            d[jc] = json.dumps(d[jc])
                    row_dicts.append(d)
                local_session.execute(insert_sql, row_dicts)

            local_session.commit()
            elapsed = time.monotonic() - t0
            print(f"  {table}: {len(rows)} rows copied ({elapsed:.1f}s)")

        total_elapsed = time.monotonic() - total_start

        # Re-enable FK triggers
        for table in TABLES:
            local_session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER ALL"))
        local_session.commit()

        print(f"\nDone in {total_elapsed:.1f}s")

    except Exception as e:
        local_session.rollback()
        print(f"\nError: {e}")
        sys.exit(1)
    finally:
        prod_session.close()
        local_session.close()
        prod_engine.dispose()
        local_engine.dispose()


if __name__ == "__main__":
    main()
