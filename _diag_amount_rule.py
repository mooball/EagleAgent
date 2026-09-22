"""READ-ONLY: is amount == qty x round(custcol_po_rate x 1.12) everywhere?

Compares every synced opportunity's lines:
  A) amount / (qty * rate)        -> 1.0 means "correct"
  B) amount / (qty * po_rate)     -> what markup did NetSuite actually use?
"""

import os
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

from includes.netsuite.client import NetSuiteClient

PROD_URL = os.environ["PROD_DATABASE_URL"].replace(
    "postgresql+asyncpg://", "postgresql+psycopg://"
).replace("postgresql://", "postgresql+psycopg://")

MAX_OPPS = 200


def main():
    eng = create_engine(PROD_URL)
    with eng.connect() as conn:
        synced = conn.execute(text(
            "SELECT r.rfq_number, r.opportunity_sync_state->>'last_synced_at' AS synced_at, "
            "       o.opportunity_number, o.netsuite_id "
            "FROM rfqs r JOIN opportunities o ON o.id = r.opportunity_id "
            "WHERE r.opportunity_sync_state IS NOT NULL "
            "  AND r.opportunity_sync_state->>'last_synced_at' IS NOT NULL "
            "  AND o.netsuite_id IS NOT NULL "
            "ORDER BY r.opportunity_sync_state->>'last_synced_at' DESC "
            "LIMIT :n"
        ), {"n": MAX_OPPS}).mappings().all()
    eng.dispose()
    print(f"Checking {len(synced)} SYNCED opportunities\n")
    opps = [{"netsuite_id": s["netsuite_id"],
             "opportunity_number": s["opportunity_number"],
             "rfq_number": s["rfq_number"]} for s in synced]

    c = NetSuiteClient()
    ratios_b: list[float] = []
    ratios_a: list[float] = []
    inconsistent: list[str] = []
    total_lines = 0

    for opp in opps:
        ns_id = str(opp["netsuite_id"])
        try:
            data = c.get(
                f"record/v1/opportunity/{ns_id}?expandSubResources=true"
            ).json()
        except Exception as exc:
            print(f"  {opp['opportunity_number']}: FAILED {exc}")
            continue
        lines = ((data.get("item") or {}).get("items") or [])
        if not lines:
            print(f"  {opp['opportunity_number']}: 0 lines")
            continue
        bad = 0
        for ln in lines:
            q = float(ln.get("quantity") or 0)
            rate = ln.get("rate")
            amt = ln.get("amount")
            po = ln.get("custcol_po_rate")
            if not q or rate is None or amt is None or po is None:
                continue
            total_lines += 1
            rate, amt, po = float(rate), float(amt), float(po)
            if abs(amt - round(q * rate, 2)) > 0.005:
                bad += 1
            if q * po:
                ratios_b.append(amt / (q * po))
            if q * rate:
                ratios_a.append(amt / (q * rate))
        print(
            f"  {opp['rfq_number']:<14} {opp['opportunity_number']:<10} "
            f"lines={len(lines):<4} wrong_amount={bad}"
        )
        if bad and len(lines) != bad:
            inconsistent.append(opp["opportunity_number"])

    print(f"\nlines examined: {total_lines}")
    if ratios_a:
        print(f"A) amount/(qty*rate):    min={min(ratios_a):.6f} "
              f"max={max(ratios_a):.6f}")
    if ratios_b:
        print(f"B) amount/(qty*po_rate): min={min(ratios_b):.6f} "
              f"max={max(ratios_b):.6f}")
    print(f"opps with SOME-but-not-all lines wrong: {inconsistent}")


if __name__ == "__main__":
    main()
