"""
brand_dedup_report.py

Read-only report of brand duplicate candidates for client review.

Groups brands by two signals and one "already done" bucket:

  Tier 1 (exact)     — identical after normalisation (case/punctuation/space)
  Tier 2 (fuzzy)     — rapidfuzz ratio >= threshold within a first-4-chars block
  Tier 3 (resolved)  — brands already marked via duplicate_of locally

Each group gets a suggested canonical: highest local product usage, then
purchase-history count, then most recent purchase, then shortest name.

Output (written into DATA_DIR):
  - brand_duplicates.csv   — one row per brand (Excel-friendly)
  - brand_duplicates.txt   — human-readable groups for emailing

Usage:
  uv run python -m scripts.brand_dedup_report                # local DB
  uv run python -m scripts.brand_dedup_report --threshold 90 # wider fuzzy net
  uv run python -m scripts.brand_dedup_report --production   # prod DB
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict

from rapidfuzz import fuzz
from sqlalchemy import create_engine, text

from config.settings import Config

_BLOCK_LEN = 4        # first N chars of the normalised name (blocking key)
_BUCKET_CAP = 400     # skip fuzzy inside larger buckets (perf guard)


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings.")
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(db_url)


def normalise_key(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    key = (name or "").lower().strip()
    key = re.sub(r"[^a-z0-9]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def _block_key(norm: str) -> str:
    """Blocking key: first N chars, also tries the name without trailing 's'
    so 'Hilti' and 'Hiltis' land in the same block."""
    base = norm.rstrip("s") if len(norm) > _BLOCK_LEN else norm
    return base[:_BLOCK_LEN]


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def load_data(engine):
    """Return (brands dict id→row, product stats, purchase stats)."""
    with engine.connect() as c:
        brand_rows = c.execute(text(
            "SELECT id::text AS id, netsuite_id, name, duplicate_of::text AS duplicate_of, isinactive "
            "FROM brands ORDER BY name"
        )).mappings().all()

        prod_rows = c.execute(text(
            "SELECT brand_id::text AS brand_id, COUNT(*) AS n "
            "FROM products WHERE brand_id IS NOT NULL GROUP BY brand_id"
        )).mappings().all()
        product_counts = {r["brand_id"]: r["n"] for r in prod_rows}

        # Purchase history via the legacy product_suppliers table
        pur_rows = c.execute(text(
            "SELECT p.brand_id::text AS brand_id, COUNT(ps.id) AS n, MAX(ps.date) AS last_date "
            "FROM product_suppliers ps JOIN products p ON p.id = ps.product_id "
            "WHERE p.brand_id IS NOT NULL "
            "GROUP BY p.brand_id"
        )).mappings().all()
        purchase = {
            r["brand_id"]: {"n": r["n"], "last": r["last_date"]}
            for r in pur_rows
        }
    return brand_rows, product_counts, purchase


def build_candidate_groups(brands, threshold: int):
    """Union-find over exact normalised keys + fuzzy pairs inside blocks."""
    by_id = {b["id"]: b for b in brands}
    norm_of = {b["id"]: normalise_key(b["name"]) for b in brands if normalise_key(b["name"])}

    blocks: dict[str, list[str]] = defaultdict(list)
    for bid, norm in norm_of.items():
        blocks[_block_key(norm)].append(bid)

    uf = UnionFind()
    pair_scores: dict[tuple[str, str], int] = {}

    for key, ids in blocks.items():
        if len(ids) > _BUCKET_CAP:
            print(f"  note: bucket '{key}' has {len(ids)} members — skipped fuzzy (cap {_BUCKET_CAP})")
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                na, nb = norm_of[a], norm_of[b]
                score = fuzz.ratio(na, nb)
                if score >= threshold:
                    uf.union(a, b)
                    pair_scores[(a, b)] = score
                elif na == nb:  # same normalised key always groups
                    uf.union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for bid in norm_of:
        groups[uf.find(bid)].append(bid)

    out = []
    for ids in groups.values():
        if len(ids) >= 2:
            out.append(ids)
    return out, pair_scores


def build_resolved_groups(brands):
    """Groups already formed by duplicate_of links."""
    by_id = {b["id"]: b for b in brands}
    canon_by_dupe: dict[str, str] = {}
    groups: dict[str, list[str]] = defaultdict(list)

    for b in brands:
        if b["duplicate_of"] and b["duplicate_of"] in by_id:
            canon = by_id[b["duplicate_of"]]["id"]
            canon_by_dupe[b["id"]] = canon
            groups[canon].append(b["id"])
    out = []
    for canon, dupes in groups.items():
        if canon not in by_id:
            continue
        out.append([canon] + dupes)
    return out, canon_by_dupe


def usage_rank(b, product_counts, purchase):
    """Sort key for canonical suggestion: usage desc, then shortest name."""
    pid = b["id"]
    pur = purchase.get(pid, {})
    return (
        -product_counts.get(pid, 0),
        -pur.get("n", 0),
        not pur.get("last"),
        len(b["name"]),
        b["name"].lower(),
    )


def suggest_canonical(members, product_counts, purchase):
    return min(members, key=lambda b: usage_rank(b, product_counts, purchase))


def tier_of(members, norm_of):
    keys = {norm_of.get(b["id"], normalise_key(b["name"])) for b in members}
    return "1_exact" if len(keys) == 1 else "2_fuzzy"


def main():
    parser = argparse.ArgumentParser(description="Brand duplicate report (read-only).")
    parser.add_argument("--production", action="store_true", help="Target the PRODUCTION database.")
    parser.add_argument("--threshold", type=int, default=92, help="Fuzzy threshold (default 92).")
    parser.add_argument("--out", default=None, help="Output prefix (default: <DATA_DIR>/brand_duplicates).")
    args = parser.parse_args()

    engine = get_engine(is_prod=args.production)
    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Loading brands and usage stats...")
    brands, product_counts, purchase = load_data(engine)
    print(f"  {len(brands)} brands loaded.")

    by_id = {b["id"]: b for b in brands}
    norm_of = {b["id"]: normalise_key(b["name"]) for b in brands}

    resolved_ids, canon_by_dupe = build_resolved_groups(brands)

    # Candidates only consider brands NOT already resolved via duplicate_of,
    # so tiers never overlap (a resolved pair shows up once, in Tier 3).
    unresolved = [b for b in brands if not b["duplicate_of"]]
    candidate_ids, pair_scores = build_candidate_groups(unresolved, args.threshold)

    rows: list[dict] = []
    txt_groups: list[dict] = []

    def add_group(members, tier, match_reason, pair_score_map):
        members = [by_id[mid] for mid in members if mid in by_id]
        members.sort(key=lambda b: b["name"].lower())
        canonical = suggest_canonical(members, product_counts, purchase)
        rows_in = []
        for b in members:
            pur = purchase.get(b["id"], {})
            already_ns = ""
            if b["id"] in canon_by_dupe:
                canon = by_id.get(canon_by_dupe[b["id"]], {})
                already_ns = canon.get("netsuite_id") or ""
            rows_in.append({
                "name": b["name"],
                "netsuite_id": b["netsuite_id"] or "",
                "local_products": product_counts.get(b["id"], 0),
                "purchase_count": pur.get("n", 0),
                "last_purchase_date": str(pur["last"]) if pur.get("last") else "",
                "already_duplicate_of_ns": already_ns,
                "isinactive": bool(b["isinactive"]),
                "is_canonical": b["id"] == canonical["id"],
            })
        txt_groups.append({
            "members": rows_in,
            "tier": tier,
            "canonical_name": canonical["name"],
            "canonical_ns": canonical["netsuite_id"] or "",
            "match_reason": match_reason,
        })

    # Tier 3 first (informational), then Tier 1, then Tier 2
    for members in resolved_ids:
        add_group(members, "3_resolved", "already_merged_locally", {})

    for members in candidate_ids:
        member_brands = [by_id[mid] for mid in members if mid in by_id]
        tier = tier_of(member_brands, norm_of)
        if tier == "1_exact":
            reason = "exact_normalised_match"
        else:
            best = 0
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    score = pair_scores.get((a, b)) or pair_scores.get((b, a))
                    if score:
                        best = max(best, score)
            reason = f"fuzzy_ratio_{best}"
        add_group(members, tier, reason, pair_scores)

    # Order groups: tier 1, tier 2, tier 3; biggest groups first
    tier_order = {"1_exact": 0, "2_fuzzy": 1, "3_resolved": 2}
    txt_groups.sort(key=lambda g: (tier_order[g["tier"]], -len(g["members"])))

    # Build CSV rows with group ids
    csv_rows = []
    group_idx = 0
    for g in txt_groups:
        group_idx += 1
        gid = group_idx
        for m in g["members"]:
            csv_rows.append({
                "group_id": gid,
                "tier": g["tier"],
                "suggested_canonical": g["canonical_name"],
                "canonical_ns_id": g["canonical_ns"],
                "name": m["name"],
                "netsuite_id": m["netsuite_id"],
                "local_products": m["local_products"],
                "purchase_count": m["purchase_count"],
                "last_purchase_date": m["last_purchase_date"],
                "already_duplicate_of_ns_id": m["already_duplicate_of_ns"],
                "inactive": "yes" if m["isinactive"] else "",
                "match_reason": g["match_reason"],
                "group_size": len(g["members"]),
                "is_suggested_canonical": "yes" if m["is_canonical"] else "",
            })
        g["group_id"] = gid

    # ---- Write CSV ----
    out_prefix = args.out or os.path.join(Config.DATA_DIR, "brand_duplicates")
    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)), exist_ok=True)
    csv_path = out_prefix + ".csv"
    fields = [
        "group_id", "tier", "suggested_canonical", "canonical_ns_id",
        "name", "netsuite_id", "local_products", "purchase_count",
        "last_purchase_date", "already_duplicate_of_ns_id", "inactive",
        "match_reason", "group_size", "is_suggested_canonical",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)

    # ---- Write TXT ----
    txt_path = out_prefix + ".txt"
    tier_labels = {
        "1_exact": "Tier 1 — exact normalised matches (case/punctuation/space only)",
        "2_fuzzy": f"Tier 2 — fuzzy matches (ratio >= {args.threshold})",
        "3_resolved": "Tier 3 — already merged locally (duplicate_of set)",
    }
    lines = []
    lines.append(f"Brand duplicate report — {env_label} database")
    lines.append(f"Generated: {__import__('datetime').datetime.now().isoformat()}")
    lines.append(f"Fuzzy threshold: {args.threshold}")
    lines.append("")
    n1 = sum(1 for g in txt_groups if g["tier"] == "1_exact")
    n2 = sum(1 for g in txt_groups if g["tier"] == "2_fuzzy")
    n3 = sum(1 for g in txt_groups if g["tier"] == "3_resolved")
    lines.append(f"Summary: {n1} exact group(s), {n2} fuzzy group(s), {n3} already-resolved group(s).")
    lines.append("Suggested canonical = most local usage (products → purchases → recency → shortest name).")
    lines.append("")

    last_tier = None
    for g in txt_groups:
        if g["tier"] != last_tier:
            last_tier = g["tier"]
            lines.append("")
            lines.append("=" * 72)
            lines.append(tier_labels[g["tier"]])
            lines.append("=" * 72)
        lines.append("")
        lines.append(
            f"Group {g['group_id']} — {len(g['members'])} brands — "
            f"suggested canonical: \"{g['canonical_name']}\" (NS {g['canonical_ns']})"
        )
        for m in g["members"]:
            marker = " ← canonical" if m["is_canonical"] else ""
            lines.append(
                f"    \"{m['name']}\"  NS {m['netsuite_id']}  "
                f"products={m['local_products']} purchases={m['purchase_count']} "
                f"last={m['last_purchase_date'] or '-'}{marker}"
            )

    with open(txt_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\nWrote {len(csv_rows)} brand rows across {len(txt_groups)} groups.")
    print(f"  CSV: {csv_path}")
    print(f"  TXT: {txt_path}")
    print(f"  Groups — exact: {n1}, fuzzy: {n2}, resolved: {n3}")


if __name__ == "__main__":
    sys.exit(main())
