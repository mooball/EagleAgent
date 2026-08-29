"""Force an RFQ's suppliers back through the DB-matching flow.

Dry run (default): probe match_supplier for each unlinked supplier and
print what WOULD happen (exact / near-miss / new). No writes.

Apply (--apply): run the real add_supplier flow for unlinked suppliers —
re-matches, creates web records + near-miss candidate rows where needed,
and persists db_match so the UI icons update on refresh.

Usage:
    uv run python _retest_rfq_match.py RFQ-2026-1046
    uv run python _retest_rfq_match.py RFQ-2026-1046 --apply
"""

import sys

sys.path.insert(0, __import__("os").getcwd())

from includes.tools.rfq_crud import _add_supplier_sync, _get_rfq_dict_sync
from includes.dashboard.database import get_session, match_supplier


def _probe(sup):
    url = None
    for c in sup.get("contacts") or []:
        if isinstance(c, dict) and c.get("url"):
            url = c["url"]
            break
    session = get_session()
    try:
        return match_supplier(
            sup.get("name", ""), url=url, country=sup.get("country"), session=session
        )
    finally:
        session.close()


def main():
    args = [a for a in sys.argv[1:]]
    apply = "--apply" in args
    rfq_number = next((a for a in args if not a.startswith("--")), "RFQ-2026-1046")

    rfq = _get_rfq_dict_sync(rfq_number)
    if not rfq:
        print(f"RFQ '{rfq_number}' not found.")
        return 1

    print(f"=== {rfq_number} — {len(rfq.get('items', []))} item(s) ===")
    for item in rfq.get("items", []):
        line = item.get("line")
        print(f"\nline {line}: {item.get('part_number') or item.get('input_description')}")
        for s in item.get("suppliers") or []:
            state = f"supplier_id={s.get('supplier_id')} db_match={s.get('db_match')} is_new={s.get('is_new')}"
            print(f"  {s.get('name')!r} [{s.get('status')}] {state}")
            if s.get("supplier_id"):
                continue
            m = _probe(s)
            if m.supplier:
                print(f"    → EXACT: {m.supplier.name!r} ({m.supplier.id})")
            for nm in m.near_misses:
                print(f"    → near-miss: {nm['supplier'].name!r} conf={nm['confidence']} because={nm['rejected_because']}")
            if not m.supplier and not m.near_misses:
                print("    → no match (would create a NEW record)")
            if apply:
                result = _add_supplier_sync(
                    rfq_number, {"line": line, "suppliers": [s]}, "retest"
                )
                if isinstance(result, str):
                    print(f"    apply error: {result}")
                else:
                    print("    applied — refresh the RFQ page to see the icon")

    if not apply:
        print("\nDry run — no changes made. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
