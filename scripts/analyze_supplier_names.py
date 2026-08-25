"""Analyse supplier name tokens to grow the matching stoplist (NOISE_TOKENS).

Read-only. For each token across supplier names + alt_names it prints
occurrence count, distinct-supplier share (how many different suppliers the
token appears in — high = generic noise candidate) and example names.

Usage:
    uv run python -m scripts.analyze_supplier_names [--limit 60]

Tokens already in NOISE_TOKENS are shown with a ✓ so the current seed list's
coverage is visible. Candidate stoplist tokens are the frequent, high-share
ones without ✓.
"""

import argparse
import logging
import re
from collections import defaultdict

from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier
from includes.dashboard.supplier_matching import NOISE_TOKENS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _raw_tokens(name: str) -> list[str]:
    return [t for t in _PUNCT_RE.sub(" ", name.lower()).split() if t]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse supplier name tokens.")
    parser.add_argument("--limit", type=int, default=60,
                        help="Number of token rows to print (default 60).")
    args = parser.parse_args()

    session = get_session()
    try:
        suppliers = session.query(Supplier.name, Supplier.alt_names).all()
    finally:
        session.close()

    # token -> {occurrences, supplier_count, examples}
    stats: dict[str, dict] = defaultdict(
        lambda: {"occurrences": 0, "suppliers": set(), "examples": []}
    )

    for name, alt_names in suppliers:
        names = [name] + [a for a in (alt_names or []) if a and not a.startswith("__")]
        for n in names:
            if not n:
                continue
            seen_in_name = set()
            for tok in _raw_tokens(n):
                if tok in seen_in_name:
                    continue  # count each token once per name variant
                seen_in_name.add(tok)
                st = stats[tok]
                st["occurrences"] += 1
                st["suppliers"].add(name or n)
                if len(st["examples"]) < 3 and n not in st["examples"]:
                    st["examples"].append(n)

    rows = []
    for token, st in stats.items():
        if token not in NOISE_TOKENS and not (len(token) == 1 and token.isalpha()):
            share = len(st["suppliers"]) / max(len(suppliers), 1)
            rows.append((token, st["occurrences"], len(st["suppliers"]), share, st["examples"]))

    rows.sort(key=lambda r: (-r[3], -r[2]))

    print(f"Suppliers scanned: {len(suppliers)}")
    print(f"{'token':<16} {'occ':>6} {'suppliers':>9} {'share':>7}  examples")
    for token, occ, sup_count, share, examples in rows[: args.limit]:
        ex = "; ".join(examples[:3])[:70]
        print(f"{token:<16} {occ:>6} {sup_count:>9} {share:>6.1%}  {ex}")

    noise_seen = sorted({t for t in stats if t in NOISE_TOKENS})
    print(f"\n{len(rows)} distinct non-stoplist tokens.")
    print(f"NOISE_TOKENS seen in data: {', '.join(noise_seen)}")


if __name__ == "__main__":
    main()
