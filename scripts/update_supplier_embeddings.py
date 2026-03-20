"""
update_supplier_embeddings.py

Generates vector embeddings for supplier notes and writes them back to the database.
Only the `notes` field is embedded — name, city, country, and brands are excluded
because they are categorical fields already handled by ilike filters in search_suppliers.

Usage:
  Local database embeddings:
    uv run python -m scripts.update_supplier_embeddings

  Production database embeddings:
    uv run python -m scripts.update_supplier_embeddings --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import time
import argparse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from config.settings import Config
from includes.db_models import Supplier


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


def main():
    parser = argparse.ArgumentParser(description="Generate and update supplier embedding vectors in the database.")
    parser.add_argument("--production", action="store_true", help="Update embeddings in the PRODUCTION database.")
    args = parser.parse_args()

    if "GOOGLE_API_KEY" not in os.environ:
        print("Error: GOOGLE_API_KEY is not set in your environment. Cannot generate embeddings.")
        return

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)

    embed_model_name = Config.EMBEDDINGS_MODEL
    print(f"Initializing Embeddings Model: {embed_model_name}")
    embeddings_model = GoogleGenerativeAIEmbeddings(model=embed_model_name, output_dimensionality=256)

    with Session() as session:
        # Fetch suppliers that have notes but no embedding yet
        suppliers = (
            session.query(Supplier)
            .filter(Supplier.embedding.is_(None), Supplier.notes.isnot(None))
            .all()
        )

        if not suppliers:
            print("No pending suppliers require embeddings. All caught up!")
            return

        # Count suppliers with no notes (for summary)
        no_notes_count = (
            session.query(Supplier)
            .filter(Supplier.notes.is_(None))
            .count()
        )

        print(f"Found {len(suppliers)} suppliers needing embeddings ({no_notes_count} skipped — no notes).")

        batch_size = 100
        for i in range(0, len(suppliers), batch_size):
            batch = suppliers[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(suppliers) + batch_size - 1) // batch_size

            print(f"Processing batch {batch_num}/{total_batches}...")

            # Embed notes only
            texts_to_embed = [s.notes for s in batch]

            # Exponential backoff for rate limits and transient connection drops
            retries = 0
            max_retries = 10
            while True:
                try:
                    batch_embeddings = embeddings_model.embed_documents(texts_to_embed)
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    transient_errors = ["429", "quota", "exhausted", "disconnected", "protocol", "503", "502", "500", "504", "timeout"]
                    if any(te in err_str for te in transient_errors):
                        retries += 1
                        if retries > max_retries:
                            print(f"Max retries ({max_retries}) exceeded. Aborting.")
                            raise
                        sleep_time = 15 * retries
                        print(f"[{retries}] Transient error: '{e}'. Sleeping {sleep_time}s...")
                        time.sleep(sleep_time)
                    else:
                        print(f"Non-transient error generating embeddings: {e}")
                        raise

            for idx, emb_vector in enumerate(batch_embeddings):
                batch[idx].embedding = emb_vector

            session.commit()
            print(f"Committed batch {batch_num}.")

            time.sleep(4)

    engine.dispose()
    print("\nSupplier embedding generation complete.")


if __name__ == "__main__":
    main()
