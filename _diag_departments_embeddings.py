"""Explore product embeddings grouped by department (read-only analysis).

Computes per-department centroids (avg embedding) and measures how well
departments separate in embedding space:
  1. population counts and centroids (SQL avg)
  2. centroid-to-centroid cosine similarity matrix
  3. intra-group coherence: sim of members to their own centroid
  4. nearest-centroid classification accuracy on a held-out sample
  5. within-group vs between-group pairwise similarities

Run: uv run python _diag_departments_embeddings.py
"""

import numpy as np
from sqlalchemy import create_engine, text

from includes.netsuite.departments import DEPARTMENT_BY_ID

URL = "postgresql+psycopg://postgres:postgres@localhost:5432/eagleagent"

# Departments with enough embedded products for the analysis.
# (5 has 1 embedding, 11 has 4, 7/8/13 have 0 — reported but skipped.)
ANALYZE_DEPS = ["1", "4", "9", "10"]

TRAIN_N = 4000   # per-department sample used to build centroids
TEST_N = 2000    # per-department held-out sample for classification
PAIR_N = 300     # per-department sample for within/between pair sims

seed = 42


def label(dept_id: str) -> str:
    d = DEPARTMENT_BY_ID.get(dept_id)
    return f"{d.label} ({dept_id})" if d else f"UNKNOWN ({dept_id})"


def parse_vec(s) -> np.ndarray:
    return np.fromstring(s.strip("[]"), sep=",", dtype=np.float32)


def sims_to_centroids(X: np.ndarray, C: np.ndarray) -> np.ndarray:
    """Cosine similarities of each row of X to each centroid row of C."""
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Cn = C / np.linalg.norm(C, axis=1, keepdims=True)
    return Xn @ Cn.T


def main():
    engine = create_engine(URL)
    with engine.connect() as c:
        # ---- 1. population counts & full-population centroids -------------
        print("=" * 78)
        print("1. POPULATION COUNTS AND CENTROIDS (avg embedding)")
        print("=" * 78)
        pop_centroids = {}  # dept_id -> np.ndarray
        rows = c.execute(text("""
            SELECT department_id, count(*) AS n, count(embedding) AS n_emb,
                   avg(embedding)::text AS centroid
            FROM products
            WHERE department_id IS NOT NULL
            GROUP BY department_id ORDER BY n_emb DESC
        """)).all()
        for r in rows:
            dept, n, n_emb, centroid = r
            print(f"  {label(dept):<22} products={n:>7}  embedded={n_emb:>7}")
            if centroid and n_emb >= 100:
                pop_centroids[dept] = parse_vec(centroid)

        # ---- 2. centroid-to-centroid similarity ---------------------------
        print()
        print("=" * 78)
        print("2. CENTROID COSINE SIMILARITY MATRIX (full population)")
        print("=" * 78)
        deps = sorted(pop_centroids, key=lambda d: ANALYZE_DEPS.index(d) if d in ANALYZE_DEPS else 99)
        C = np.stack([pop_centroids[d] for d in deps])
        Cn = C / np.linalg.norm(C, axis=1, keepdims=True)
        S = Cn @ Cn.T
        hdr = "department".ljust(22)
        for d in deps:
            hdr += f"{DEPARTMENT_BY_ID[d].label[:10]:>12}"
        print(hdr)
        for i, d in enumerate(deps):
            line = label(d).ljust(22)
            for j in range(len(deps)):
                line += f"{S[i, j]:>12.3f}"
            print(line)

        # ---- fetch samples -------------------------------------------------
        print()
        print("=" * 78)
        print(f"3. SAMPLING ({TRAIN_N} train / {TEST_N} test / {PAIR_N} pairs per dept)")
        print("=" * 78)
        train = {}
        test = {}
        pair = {}
        for d in ANALYZE_DEPS:
            rows = c.execute(text("""
                SELECT embedding::text FROM products
                WHERE department_id = :d AND embedding IS NOT NULL
                ORDER BY random() LIMIT :lim
            """), {"d": d, "lim": TRAIN_N + TEST_N + PAIR_N}).all()
            X = np.stack([parse_vec(r[0]) for r in rows])
            train[d] = X[:TRAIN_N]
            test[d] = X[TRAIN_N:TRAIN_N + TEST_N]
            pair[d] = X[TRAIN_N + TEST_N:]
            print(f"  {label(d):<22} sampled {len(X)}")

        # embedding norm sanity check
        norms = np.linalg.norm(np.concatenate([train[d][:200] for d in ANALYZE_DEPS]), axis=1)
        print(f"  embedding L2 norm: mean={norms.mean():.4f} min={norms.min():.4f} max={norms.max():.4f}")

        # ---- 4. train centroids + intra-group coherence --------------------
        print()
        print("=" * 78)
        print("4. INTRA-GROUP COHERENCE (sim of members to own centroid)")
        print("=" * 78)
        train_centroids = {d: train[d].mean(axis=0) for d in ANALYZE_DEPS}
        Ctr = np.stack([train_centroids[d] for d in ANALYZE_DEPS])
        order = ANALYZE_DEPS
        print(f"  {'department':<22}{'mean sim to own':>16}{'mean to best other':>18}{'margin':>9}{'% own nearest':>14}")
        for i, d in enumerate(order):
            S_all = sims_to_centroids(test[d], Ctr)
            own = S_all[:, i]
            others = np.delete(S_all, i, axis=1)
            best_other = others.max(axis=1)
            margin = own - best_other
            print(f"  {label(d):<22}{own.mean():>16.4f}{best_other.mean():>18.4f}"
                  f"{margin.mean():>9.4f}{(margin > 0).mean() * 100:>13.1f}%")

        # ---- 4b. margin percentiles (size of the "unsure zone") ----------
        print()
        print("  Margin percentiles (sim to own centroid - sim to best other)")
        print(f"  {'department':<22}{'p5':>8}{'p25':>8}{'median':>8}{'p75':>8}{'% margin<0.05':>13}")
        for i, d in enumerate(order):
            S_all = sims_to_centroids(test[d], Ctr)
            own = S_all[:, i]
            others = np.delete(S_all, i, axis=1)
            margin = np.sort(own - others.max(axis=1))
            q = np.percentile(margin, [5, 25, 50, 75])
            print(f"  {label(d):<22}{q[0]:>8.3f}{q[1]:>8.3f}{q[2]:>8.3f}{q[3]:>8.3f}"
                  f"{(margin < 0.05).mean() * 100:>12.1f}%")

        # ---- 5. nearest-centroid classification ----------------------------
        print()
        print("=" * 78)
        print("5. NEAREST-CENTROID CLASSIFICATION (held-out sample)")
        print("=" * 78)
        all_test = np.concatenate([test[d] for d in order])
        true_labels = np.concatenate([np.full(len(test[d]), i) for i, d in enumerate(order)])
        S_all = sims_to_centroids(all_test, Ctr)
        pred = S_all.argmax(axis=1)
        conf = np.zeros((len(order), len(order)), dtype=int)
        for t, p in zip(true_labels, pred):
            conf[t, p] += 1
        print(f"  {'actual\\predicted':<20}" + "".join(f"{DEPARTMENT_BY_ID[d].label[:9]:>12}" for d in order))
        for i, d in enumerate(order):
            print(f"  {label(d):<20}" + "".join(f"{v:>12}" for v in conf[i]))
        acc = (pred == true_labels).mean() * 100
        print(f"\n  overall accuracy: {acc:.1f}%  (chance with 4 balanced groups = 25%)")

        # ---- 6. within vs between pairwise similarities --------------------
        print()
        print("=" * 78)
        print("6. WITHIN-GROUP vs BETWEEN-GROUP PAIRWISE SIMILARITY (300 items/dept)")
        print("=" * 78)
        rng = np.random.default_rng(seed)
        def pair_sims(A, B, n):
            A = A[rng.choice(len(A), n)] if len(A) > n else A
            B = B[rng.choice(len(B), n)] if len(B) > n else B
            A = A / np.linalg.norm(A, axis=1, keepdims=True)
            B = B / np.linalg.norm(B, axis=1, keepdims=True)
            return (A @ B.T).ravel()

        print(f"  {'comparison':<34}{'mean sim':>10}{'std':>8}")
        for d in order:
            w = pair_sims(pair[d], pair[d], 600)
            print(f"  {('within ' + label(d)):<34}{w.mean():>10.4f}{w.std():>8.4f}")
        for a, d1 in enumerate(order):
            for d2 in order[a + 1:]:
                b = pair_sims(pair[d1], pair[d2], 600)
                print(f"  {('between ' + label(d1) + ' / ' + label(d2)):<34}{b.mean():>10.4f}{b.std():>8.4f}")

    engine.dispose()


if __name__ == "__main__":
    main()
