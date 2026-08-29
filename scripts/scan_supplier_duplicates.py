"""Scan all suppliers for duplicate candidates (Track A).

Replaces the O(N×M) Python loop in scripts/find_duplicate_suppliers.py with
two set-based self-joins over supplier_match_keys:

  1. Fuzzy name pairs via pg_trgm `%` (GIN index-backed)
  2. Shared-domain pairs via key equality

Modes:
    (default)  score pairs, upsert into supplier_duplicate_candidates
    --report   print a confidence histogram with sample pairs, write NOTHING
    --dry-run  score pairs, print a summary, write nothing

Usage:
    uv run python -m scripts.scan_supplier_duplicates --report
    uv run python -m scripts.scan_supplier_duplicates --min-confidence 0.7
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier, SupplierDuplicateCandidate
from includes.dashboard.supplier_dedup import pick_keep_remove
from includes.dashboard.supplier_matching import currency_tokens

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TRIGRAM_FLOOR = 0.45       # `%` operator threshold for the candidate pass
# Local histogram (2026-08-25, ~20k pairs): ~420 pairs ≥ 0.75, then noise
# explodes at 0.70-0.75 (1530 pairs). Tune with --report after stoplist changes.
DEFAULT_MIN_CONFIDENCE = 0.75
CERTAIN_SIM = 0.8


@dataclass
class PairInfo:
    name_sim: float | None = None          # max pg_trgm similarity of name keys
    name_keys_equal: bool = False          # some name key is exactly equal
    shared_domains: set = field(default_factory=set)
    domains_a: set = field(default_factory=set)
    domains_b: set = field(default_factory=set)
    country_a: str | None = None
    country_b: str | None = None
    currency_a: set = field(default_factory=set)
    currency_b: set = field(default_factory=set)


def scan_pairs(session, trigram_floor: float = TRIGRAM_FLOOR) -> dict[tuple, PairInfo]:
    """Run both candidate queries; return {(id_a, id_b): PairInfo}.

    Only supplier pairs where neither side is flagged `use_instead` are
    considered. Does not write.
    """
    # SET can't take bind params; float() guarantees a numeric literal
    session.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {float(trigram_floor)}"))

    pairs: dict[tuple, PairInfo] = defaultdict(PairInfo)

    name_rows = session.execute(text("""
        SELECT a.supplier_id AS id_a, b.supplier_id AS id_b,
               max(similarity(a.key_value, b.key_value)) AS sim,
               bool_or(a.key_value = b.key_value) AS exact_match
        FROM supplier_match_keys a
        JOIN supplier_match_keys b
          ON a.supplier_id < b.supplier_id
         AND a.key_type = 'name' AND b.key_type = 'name'
         AND a.key_value % b.key_value
        JOIN suppliers sa ON sa.id = a.supplier_id AND sa.use_instead IS NULL
        JOIN suppliers sb ON sb.id = b.supplier_id AND sb.use_instead IS NULL
        GROUP BY a.supplier_id, b.supplier_id
    """))
    for row in name_rows:
        info = pairs[(row.id_a, row.id_b)]
        info.name_sim = float(row.sim)
        info.name_keys_equal = bool(row.exact_match)

    domain_rows = session.execute(text("""
        SELECT a.supplier_id AS id_a, b.supplier_id AS id_b, a.key_value AS domain_key
        FROM supplier_match_keys a
        JOIN supplier_match_keys b
          ON a.supplier_id < b.supplier_id
         AND a.key_type = 'domain' AND b.key_type = 'domain'
         AND a.key_value = b.key_value
        JOIN suppliers sa ON sa.id = a.supplier_id AND sa.use_instead IS NULL
        JOIN suppliers sb ON sb.id = b.supplier_id AND sb.use_instead IS NULL
    """))
    for row in domain_rows:
        pairs[(row.id_a, row.id_b)].shared_domains.add(row.domain_key)

    # One batched lookup for countries, currencies and per-side domain keys
    ids = {i for pair in pairs for i in pair}
    if ids:
        rows = session.execute(
            text("SELECT id, country, name FROM suppliers WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        ).fetchall()
        countries = {r.id: r.country for r in rows}
        currencies = {r.id: currency_tokens(r.name) for r in rows}
        for (id_a, id_b), info in pairs.items():
            info.country_a = countries.get(id_a)
            info.country_b = countries.get(id_b)
            info.currency_a = currencies.get(id_a, set())
            info.currency_b = currencies.get(id_b, set())

        dk_rows = session.execute(
            text("""
                SELECT supplier_id, key_value FROM supplier_match_keys
                WHERE key_type = 'domain' AND supplier_id = ANY(:ids)
            """),
            {"ids": list(ids)},
        ).fetchall()
        dk_map: dict = {}
        for row in dk_rows:
            dk_map.setdefault(row.supplier_id, set()).add(row.key_value)
        for (id_a, id_b), info in pairs.items():
            info.domains_a = set(dk_map.get(id_a, ()))
            info.domains_b = set(dk_map.get(id_b, ()))

    return dict(pairs)


def score_pair(info: PairInfo) -> tuple[float, list[str], str]:
    """Confidence (0-1), reasons, tier ('certain' | 'review').

    Note on word-swapped names: pg_trgm similarity of two-word names with
    the words reversed is 1.0 (identical trigram sets), so trigram-only
    similarity is capped below the 'identical key' confidence.

    Note on domains: two businesses that each publish a *different*
    non-free domain are almost never the same company, however similar the
    names read. That contradiction caps confidence and forces human review
    regardless of which evidence produced the pair.
    """
    sim = info.name_sim
    shared = bool(info.shared_domains)
    domains_disagree = bool(info.domains_a and info.domains_b and not shared)

    if info.name_keys_equal:
        confidence, reasons = 0.98, ["normalised_name_identical"]
    elif shared and sim is not None:
        confidence, reasons = max(0.75, min(sim, 0.92)), ["shared_domain", f"name_similarity:{sim:.2f}"]
    elif shared:
        confidence, reasons = 0.7, ["shared_domain_only"]
    elif sim is not None:
        confidence, reasons = min(sim, 0.9), [f"name_similarity:{sim:.2f}"]
    else:
        confidence, reasons = 0.0, ["unknown"]

    if domains_disagree:
        confidence = min(confidence, 0.5)
        reasons.append("domain_disagreement")

    # Different currency annotations mark deliberately separate vendor records
    if info.currency_a and info.currency_b and not (info.currency_a & info.currency_b):
        confidence = min(confidence, 0.5)
        reasons.append("currency_mismatch")

    ca, cb = (info.country_a or "").strip().upper(), (info.country_b or "").strip().upper()
    if ca and cb and ca != cb:
        confidence = min(confidence, 0.55)
        reasons.append("country_mismatch")

    tier = candidate_tier(confidence, reasons)
    return round(confidence, 3), reasons, tier


def candidate_tier(confidence: float | None, reasons: list | None) -> str:
    """Tier for a stored candidate row — 'certain' is bulk-confirmable."""
    reasons = reasons or []
    if "domain_disagreement" in reasons or "currency_mismatch" in reasons:
        return "review"
    if "normalised_name_identical" in reasons:
        return "certain"
    if "shared_domain" in reasons and (confidence or 0) >= CERTAIN_SIM:
        return "certain"
    return "review"


def _supplier_names(session, supplier_ids) -> dict:
    if not supplier_ids:
        return {}
    rows = session.execute(
        text("SELECT id, name FROM suppliers WHERE id = ANY(:ids)"),
        {"ids": list(supplier_ids)},
    ).fetchall()
    return {r.id: r.name for r in rows}


def _report(session, pairs: dict) -> None:
    scored = []
    for (id_a, id_b), info in pairs.items():
        confidence, reasons, tier = score_pair(info)
        scored.append((id_a, id_b, confidence, reasons, tier))

    scored.sort(key=lambda r: -r[2])
    names = _supplier_names(session, {i for r in scored for i in (r[0], r[1])})

    buckets = defaultdict(lambda: [0, []])
    for id_a, id_b, confidence, reasons, tier in scored:
        bucket = min(int(confidence * 20), 19)  # 0.05-wide buckets up to 1.0
        key = f"{bucket * 0.05:.2f}-{min(bucket * 0.05 + 0.05, 1.0):.2f}"
        buckets[key][0] += 1
        if len(buckets[key][1]) < 5:
            buckets[key][1].append(
                (id_a, id_b, confidence, reasons, tier)
            )

    print(f"\nTotal candidate pairs: {len(scored)}")
    print(f"{'confidence':<13} {'count':>6}   samples")
    for key in sorted(buckets, reverse=True):
        count, samples = buckets[key]
        print(f"{key:<13} {count:>6}")
        for id_a, id_b, confidence, reasons, tier in samples:
            na, nb = names.get(id_a, "?"), names.get(id_b, "?")
            print(f"    [{tier}] {na[:45]!r}  ↔  {nb[:45]!r}  ({confidence}) {', '.join(reasons)}")


def _txn_stats(session, supplier_ids) -> dict:
    """{supplier_id: (txn_count, latest_txn_date)} in one batched query."""
    if not supplier_ids:
        return {}
    rows = session.execute(
        text("""
            SELECT supplier_id, count(*) AS cnt, max(date) AS latest
            FROM product_suppliers
            WHERE supplier_id = ANY(:ids)
            GROUP BY supplier_id
        """),
        {"ids": list(supplier_ids)},
    ).fetchall()
    return {r.supplier_id: (r.cnt, r.latest) for r in rows}


def _upsert_candidates(session, pairs: dict, min_confidence: float, user: str = "scan") -> dict:
    now = datetime.now(timezone.utc)
    existing = {
        frozenset((c.primary_id, c.duplicate_id)): c
        for c in session.query(SupplierDuplicateCandidate).all()
    }
    stats = _txn_stats(session, {i for pair in pairs for i in pair})

    created = updated = removed = skipped = 0
    for (id_a, id_b), info in pairs.items():
        confidence, reasons, tier = score_pair(info)
        row = existing.get(frozenset((id_a, id_b)))
        if confidence < min_confidence:
            # Pair no longer clears the bar — drop a stale proposed row so
            # old confidence/reasons never linger in the queue.
            if row and row.status == "proposed":
                session.delete(row)
                removed += 1
            else:
                skipped += 1
            continue

        sup_a = session.get(Supplier, id_a)
        sup_b = session.get(Supplier, id_b)
        if not sup_a or not sup_b:
            skipped += 1
            continue
        primary, duplicate = pick_keep_remove(sup_a, sup_b, stats)

        if row:
            if row.status == "proposed":
                row.confidence = confidence
                row.reasons = reasons
                # Re-evaluate primary/duplicate with the latest activity data
                row.primary_id = primary.id
                row.duplicate_id = duplicate.id
                updated += 1
            else:
                skipped += 1  # already merged/rejected — leave the decision alone
            continue

        session.add(SupplierDuplicateCandidate(
            primary_id=primary.id,
            duplicate_id=duplicate.id,
            source="auto",
            status="proposed",
            confidence=confidence,
            reasons=reasons,
            created_by=user,
            created_at=now,
        ))
        created += 1

    # Proposed rows for pairs the scan no longer produces at all (scoring
    # changed, names edited, one side merged away) would otherwise keep
    # their stale confidence in the queue forever.
    current = {frozenset(pair) for pair in pairs}
    for key, row in existing.items():
        if row.status == "proposed" and key not in current:
            session.delete(row)
            removed += 1

    return {"created": created, "updated": updated, "removed": removed, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan suppliers for duplicate candidates.")
    parser.add_argument("--report", action="store_true",
                        help="Print confidence histogram with samples; write nothing.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score pairs and print a summary; write nothing.")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                        help=f"Minimum confidence to record (default {DEFAULT_MIN_CONFIDENCE}).")
    parser.add_argument("--trigram-floor", type=float, default=TRIGRAM_FLOOR,
                        help=f"pg_trgm candidate threshold (default {TRIGRAM_FLOOR}).")
    args = parser.parse_args()

    session = get_session()
    try:
        pairs = scan_pairs(session, args.trigram_floor)
        logger.info(f"Candidate pair query returned {len(pairs)} pairs")

        if args.report or args.dry_run:
            _report(session, pairs)
            if args.dry_run:
                summary = _count_above(pairs, args.min_confidence)
                print(f"\nWould record {summary} candidate(s) at min-confidence {args.min_confidence}.")
            return

        result = _upsert_candidates(session, pairs, args.min_confidence)
        session.commit()
        logger.info(f"Candidates: {result}")
    finally:
        session.close()


def _count_above(pairs: dict, min_confidence: float) -> int:
    return sum(1 for info in pairs.values() if score_pair(info)[0] >= min_confidence)


if __name__ == "__main__":
    main()
