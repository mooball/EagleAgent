"""
cluster_products.py — Phase 1 of Product Category Discovery

Loads product embeddings from the local database, runs MiniBatchKMeans clustering
with a target of 100 clusters, samples representative products from each
cluster, and writes the results to data/product_clusters.json for LLM labelling.

Usage:
  uv run python -m scripts.cluster_products
  uv run python -m scripts.cluster_products --n-clusters 150
"""

import json
import argparse
import sys
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import normalize

from config.settings import Config
from includes.dashboard.models import Product


def get_engine():
    """Return a SQLAlchemy engine for the local database."""
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(db_url)


def load_embeddings(session, batch_size: int = 5000):
    """Load all product embeddings from the database in batches.

    Returns:
        embeddings:  np.ndarray of shape (n_products, 256), float32
        product_ids: list of str (UUID hex)
        part_numbers: list of str
        descriptions: list of str
        brands: list of str
    """
    count = session.query(Product).filter(Product.embedding.isnot(None)).count()
    print(f"Products with embeddings: {count:,}")

    embeddings_list = []
    product_ids = []
    part_numbers = []
    descriptions = []
    brands = []

    offset = 0
    while True:
        batch = (
            session.query(Product)
            .filter(Product.embedding.isnot(None))
            .order_by(Product.id)
            .offset(offset)
            .limit(batch_size)
            .all()
        )

        if not batch:
            break

        for p in batch:
            # pgvector Vector is list-like; cast to list for numpy
            vec = list(p.embedding) if p.embedding is not None else None
            if vec is None or len(vec) != 256:
                continue
            embeddings_list.append(vec)
            product_ids.append(str(p.id))
            part_numbers.append(p.part_number or "")
            descriptions.append(p.description or "")
            brands.append(p.brand or "")

        offset += len(batch)
        print(f"  Loaded {offset:,} / {count:,} products...")

    if not embeddings_list:
        print("ERROR: No embeddings found in the database.")
        sys.exit(1)

    embeddings_array = np.array(embeddings_list, dtype=np.float32)
    print(f"Embeddings shape: {embeddings_array.shape}")

    return embeddings_array, product_ids, part_numbers, descriptions, brands


def cluster_and_sample(
    embeddings_array: np.ndarray,
    product_ids: list[str],
    part_numbers: list[str],
    descriptions: list[str],
    brands: list[str],
    n_clusters: int = 100,
    n_close: int = 7,
    n_edge: int = 3,
    random_state: int = 42,
):
    """Run MiniBatchKMeans and sample representative products per cluster.

    Returns a list of cluster dicts for JSON serialisation.
    """
    print(f"\nNormalising {embeddings_array.shape[0]:,} vectors (L2 norm)...")
    embeddings_norm = normalize(embeddings_array, norm="l2")

    print(f"Running MiniBatchKMeans (n_clusters={n_clusters})...")
    kmeans = MiniBatchKMeans(
        n_clusters=n_clusters,
        random_state=random_state,
        batch_size=min(4096, embeddings_array.shape[0] // 10),
        max_iter=100,
        n_init=3,
        verbose=1,
    )
    cluster_labels = kmeans.fit_predict(embeddings_norm)
    centroids = kmeans.cluster_centers_

    # Compute inertia (within-cluster sum of squares) as a sanity check
    inertia = kmeans.inertia_
    print(f"K-Means inertia: {inertia:,.2f}")

    # ---- Build cluster summaries ----
    print("Sampling products per cluster...")
    clusters = []

    for cluster_id in range(n_clusters):
        mask = cluster_labels == cluster_id
        cluster_indices = np.where(mask)[0]
        cluster_size = len(cluster_indices)

        if cluster_size == 0:
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "size": 0,
                    "avg_distance": 0.0,
                    "samples": [],
                }
            )
            continue

        # Distances to centroid (cosine distance ≈ euclidean on L2-normalised vectors)
        cluster_vectors = embeddings_norm[cluster_indices]
        distances = np.linalg.norm(cluster_vectors - centroids[cluster_id], axis=1)
        avg_distance = float(np.mean(distances))

        # Sort by distance ascending
        sorted_order = np.argsort(distances)
        sorted_indices = cluster_indices[sorted_order]
        sorted_distances = distances[sorted_order]

        # ---- Sample products ----
        sample_indices = []
        sample_distances = []

        # 1) Closest n_close products
        n_c = min(n_close, cluster_size)
        for i in range(n_c):
            sample_indices.append(int(sorted_indices[i]))
            sample_distances.append(float(sorted_distances[i]))

        # 2) Boundary products at 70th-90th percentile
        if cluster_size > n_close + n_edge:
            p70 = int(cluster_size * 0.70)
            p90 = int(cluster_size * 0.90)
            edge_range = p90 - p70
            if edge_range >= n_edge:
                positions = np.linspace(p70, p90 - 1, n_edge, dtype=int)
            else:
                # Small range — take from the farthest end
                positions = np.arange(
                    max(0, cluster_size - n_edge - n_close), cluster_size - n_close
                )[:n_edge]
            for pos in positions:
                sample_indices.append(int(sorted_indices[pos]))
                sample_distances.append(float(sorted_distances[pos]))
        elif cluster_size > n_close:
            # Very small cluster — grab remaining farthest
            remaining = cluster_size - n_c
            for i in range(remaining):
                idx = cluster_size - 1 - i
                sample_indices.append(int(sorted_indices[idx]))
                sample_distances.append(float(sorted_distances[idx]))

        samples = []
        for idx, dist in zip(sample_indices, sample_distances):
            samples.append(
                {
                    "id": product_ids[idx],
                    "part_number": part_numbers[idx],
                    "description": descriptions[idx],
                    "brand": brands[idx],
                    "distance": round(dist, 4),
                }
            )

        clusters.append(
            {
                "cluster_id": cluster_id,
                "size": cluster_size,
                "avg_distance": round(avg_distance, 4),
                "samples": samples,
            }
        )

    return clusters, inertia


def main():
    parser = argparse.ArgumentParser(
        description="Cluster product embeddings for category discovery (Phase 1)."
    )
    parser.add_argument(
        "--n-clusters",
        type=int,
        default=100,
        help="Number of K-Means clusters (default: 100).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Database fetch batch size (default: 5000).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Custom output path (default: data/product_clusters.json).",
    )
    args = parser.parse_args()

    print("Connecting to database...")
    engine = get_engine()
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # ---- Load embeddings ----
        embeddings_array, product_ids, part_numbers, descriptions, brands = (
            load_embeddings(session, batch_size=args.batch_size)
        )

    # ---- Cluster & Sample ----
    clusters, inertia = cluster_and_sample(
        embeddings_array,
        product_ids,
        part_numbers,
        descriptions,
        brands,
        n_clusters=args.n_clusters,
    )

    # ---- Write output ----
    data_dir = Path(Config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output or str(data_dir / "product_clusters.json")

    with open(output_path, "w") as f:
        json.dump(clusters, f, indent=2, ensure_ascii=False)

    print(f"\nOutput written to: {output_path}")
    print(f"Total products clustered: {embeddings_array.shape[0]:,}")
    print(f"Inertia: {inertia:,.2f}")

    # Summary stats
    sizes = [c["size"] for c in clusters if c["size"] > 0]
    if sizes:
        print(f"Non-empty clusters: {len(sizes)}")
        print(f"Empty clusters:     {sum(1 for c in clusters if c['size'] == 0)}")
        print(f"Cluster size — min: {min(sizes):,}  max: {max(sizes):,}  "
              f"mean: {np.mean(sizes):,.0f}  median: {np.median(sizes):,.0f}")


if __name__ == "__main__":
    main()
