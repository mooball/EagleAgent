"""
update_product_embeddings.py

Fetches product records from the database that are missing embeddings,
queries Google Generative AI to generate vectors based on their product details,
and writes the embeddings back to the database.

Usage:
  Local database embeddings:
    uv run python -m scripts.update_product_embeddings
    
  Production database embeddings:
    uv run python -m scripts.update_product_embeddings --production
    (Requires PROD_DATABASE_URL to be set in your .env file)
"""

import os
import time
import argparse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from config.settings import Config
from includes.db_models import Product

def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings (e.g. PROD_DATABASE_URL).")
        
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(db_url)

def main():
    parser = argparse.ArgumentParser(description="Generate and update product vectors in the database.")
    parser.add_argument("--production", action="store_true", help="Update embeddings in the PRODUCTION database.")
    args = parser.parse_args()

    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") != "true":
        print("Error: GOOGLE_GENAI_USE_VERTEXAI is not set. Configure Vertex AI env vars first.")
        return

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)
    
    embed_model_name = Config.EMBEDDINGS_MODEL
    print(f"Initializing Embeddings Model: {embed_model_name}")
    embeddings_model = GoogleGenerativeAIEmbeddings(model=embed_model_name, output_dimensionality=256)
    
    with Session() as session:
        # Fetch products without embeddings
        products = session.query(Product).filter(Product.embedding.is_(None)).all()
        
        if not products:
            print("No pending products require embeddings. All caught up!")
            return
            
        print(f"Found {len(products)} products needing embeddings.")
        
        batch_size = 100
        for i in range(0, len(products), batch_size):
            batch_products = products[i:i+batch_size]
            
            print(f"Processing batch {i // batch_size + 1}/{(len(products) + batch_size - 1) // batch_size}...")
            
            texts_to_embed = []
            for p in batch_products:
                embed_parts = []
                if p.part_number: embed_parts.append(f"Part Number: {p.part_number}")
                if p.description: embed_parts.append(f"Description: {p.description}")
                if p.brand: embed_parts.append(f"Brand: {p.brand}")
                
                embed_text = " | ".join(embed_parts)
                # Fallback just in case all parts are empty
                if not embed_text.strip():
                    embed_text = "Unknown Product"
                
                texts_to_embed.append(embed_text)

            # Exponential backoff for rate limits and transient connection drops
            retries = 0
            max_retries = 10
            while True:
                try:
                    batch_embeddings = embeddings_model.embed_documents(texts_to_embed)
                    break  # Success
                except Exception as e:
                    err_str = str(e).lower()
                    transient_errors = ["429", "quota", "exhausted", "disconnected", "protocol", "503", "502", "500", "504", "timeout"]
                    if any(te in err_str for te in transient_errors):
                        retries += 1
                        if retries > max_retries:
                            print(f"Max retries ({max_retries}) exceeded for transient error. Aborting script to prevent infinite hang.")
                            raise
                        sleep_time = 15 * retries
                        print(f"[{retries}] Hit API or connection issue: '{e}'. Sleeping for {sleep_time} seconds before retrying...")
                        time.sleep(sleep_time)
                    else:
                        print(f"Non-transient error generating embeddings: {e}")
                        raise
            
            # Apply embeddings back to objects
            for idx, emb_vector in enumerate(batch_embeddings):
                batch_products[idx].embedding = emb_vector
                
            # Commit the batch incrementally
            session.commit()
            print(f"Successfully committed batch {i // batch_size + 1}.")
            
            # Sleep slightly to avoid spamming the free tier limits
            time.sleep(4)
            
    print("\nVector embedding ingestion complete.")

if __name__ == "__main__":
    main()
