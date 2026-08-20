"""Probe NetSuite REST API for opportunity line-item capability (READ-ONLY).

Answers:
  1. Full opportunity schema (Accept: application/schema+json) — line fields,
     sublists, mandatory fields
  2. Whether our own RFQ-linked opportunities have lines, and what they look like
  3. Custom PO fields (custcol_po_vendor / custcol_po_rate) on opportunity lines
  4. The REST GET shape of an opportunity's 'item' sublist

Run: uv run python -u _probe_ns_opp_items.py
"""

import json

import requests
from sqlalchemy import create_engine, text

from includes.netsuite.client import NetSuiteClient

LOCAL_DB = "postgresql+psycopg://postgres:postgres@localhost:5432/eagleagent"


def section(title):
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}", flush=True)


def main():
    c = NetSuiteClient()

    # ------------------------------------------------------------------
    section("1. OPPORTUNITY SCHEMA (Accept: application/schema+json)")
    # ------------------------------------------------------------------
    try:
        headers = c._headers()
        headers["Accept"] = "application/schema+json"
        r = c._session.get(
            f"{c._base_url}/record/v1/metadata-catalog/opportunity",
            headers=headers, timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        print("top-level keys:", list(data.keys()), flush=True)
        for key in ("operations", "properties", "sublists", "subrecords"):
            if key in data:
                print(f"\n-- {key}:")
                val = data[key]
                if isinstance(val, dict):
                    for name, spec in list(val.items())[:120]:
                        if isinstance(spec, dict):
                            t = spec.get("type") or spec.get("$ref") or ""
                            print(f"   {name}: {t} mandatory={spec.get('mandatory')}")
                        else:
                            print(f"   {name}: {spec}")
                else:
                    print(json.dumps(val, indent=1)[:1200])
        # dump raw json to file for later inspection
        with open("_ns_opp_schema.json", "w") as f:
            json.dump(data, f, indent=2)
        print("\n(full schema saved to _ns_opp_schema.json)")
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e)[:400], flush=True)

    # ------------------------------------------------------------------
    section("2. LOCAL: our RFQ-linked opportunities")
    # ------------------------------------------------------------------
    opp_ids = []
    eng = create_engine(LOCAL_DB)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT netsuite_id, opportunity_number, title, status "
            "FROM opportunities ORDER BY netsuite_id DESC LIMIT 15"
        )).all()
        print(f"local opportunities: {len(rows)}")
        for r in rows:
            print("  ", dict(r._mapping))
            if r.netsuite_id:
                opp_ids.append(r.netsuite_id)
    eng.dispose()

    # ------------------------------------------------------------------
    section("3. SUITEQL: lines on those opportunities (targeted)")
    # ------------------------------------------------------------------
    found = []
    for opp_id in opp_ids:
        try:
            rows = c.suiteql(
                "SELECT tl.id, tl.item, BUILTIN.DF(tl.item) AS item_name, tl.description, "
                "tl.quantity, tl.rate, tl.amount, tl.department, BUILTIN.DF(tl.department) AS dept_name "
                "FROM transactionLine tl "
                f"WHERE tl.transaction = '{opp_id}' AND tl.mainline = 'F' AND tl.taxline = 'F'",
                limit=20,
            )
            print(f"opp {opp_id}: {len(rows)} non-main lines", flush=True)
            for row in rows[:5]:
                print("   ", row)
            if rows:
                found.append(opp_id)
        except Exception as e:
            print(f"opp {opp_id}: {type(e).__name__} {str(e)[:200]}", flush=True)

    if not found:
        # fall back to a plain (unsorted) scan of recent opportunities
        print("\n(none of our opportunities have lines — trying recent net-new ones)")
        try:
            rows = c.suiteql(
                "SELECT t.id, t.tranid FROM transaction t WHERE t.type = 'Opprtnty'",
                limit=10,
            )
            for r in rows:
                opp_id = r["id"]
                try:
                    lines = c.suiteql(
                        "SELECT tl.id, tl.item, BUILTIN.DF(tl.item) AS item_name, tl.description, "
                        "tl.quantity, tl.rate, tl.amount, tl.department "
                        "FROM transactionLine tl "
                        f"WHERE tl.transaction = '{opp_id}' AND tl.mainline = 'F' AND tl.taxline = 'F'",
                        limit=20,
                    )
                    print(f"opp {opp_id} ({r.get('tranid')}): {len(lines)} lines", flush=True)
                    for row in lines[:5]:
                        print("   ", row)
                    if lines:
                        found.append(opp_id)
                        break
                except Exception as e:
                    print(f"opp {opp_id}: {type(e).__name__} {str(e)[:150]}", flush=True)
        except Exception as e:
            print("FAILED:", type(e).__name__, str(e)[:300], flush=True)

    if not found:
        print("\n(no opportunity with lines found at all)")
        return

    opp_id = found[0]

    # ------------------------------------------------------------------
    section("4. SUITEQL: custom PO fields on opportunity lines")
    # ------------------------------------------------------------------
    try:
        rows = c.suiteql(
            "SELECT tl.custcol_po_vendor, BUILTIN.DF(tl.custcol_po_vendor) AS vendor_name, "
            "tl.custcol_po_rate "
            "FROM transactionLine tl "
            f"WHERE tl.transaction = '{opp_id}' AND tl.mainline = 'F' AND tl.taxline = 'F'",
            limit=20,
        )
        print(f"custom PO fields: {len(rows)} rows", flush=True)
        for r in rows[:5]:
            print("   ", r)
    except Exception as e:
        print(f"custom PO fields: {type(e).__name__} {str(e)[:300]}", flush=True)

    # ------------------------------------------------------------------
    section("5. RECORD API GET: opportunity with lines (raw 'item' shape)")
    # ------------------------------------------------------------------
    try:
        data = c.get(f"record/v1/opportunity/{opp_id}").json()
        print(f"opportunity {opp_id}: keys={list(data.keys())}")
        item = data.get("item")
        if item is not None:
            print("item sublist:", json.dumps(item, indent=2)[:3000])
        else:
            print("no 'item' key in response")
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e)[:300], flush=True)


if __name__ == "__main__":
    main()
