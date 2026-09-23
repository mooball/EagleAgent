"""Repair dangling ``supplier_id`` references in RFQ JSONB (RFQ-2026-xxxx).

The ProcurementAgent can invent UUID-shaped supplier ids; they pass the shape
check and get persisted as if they were real links. 2026-09-23: 26 such refs
across 5 RFQs. One of them 500'd the whole RFQ detail page once it was marked
selected (RFQ-2026-2111, fixed separately).

This repairs the rest by exact case-insensitive name match against `suppliers`.
Anything ambiguous or unresolvable is reported and left alone — it will render
with a ⚠ warning rather than crashing.

DRY RUN by default. Pass --apply to write (single transaction, with a JSON
backup of every touched row written first).

Usage:
    uv run python scripts/repair_orphan_supplier_ids.py            # dry run
    uv run python scripts/repair_orphan_supplier_ids.py --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from sqlalchemy import create_engine, text

BACKUP_PATH = "/tmp/orphan_supplier_id_repair_backup.json"

# JSONB columns on rfq_items that hold arrays of {supplier_id, name, ...}
COLUMNS = ("suppliers", "brand_suppliers")

ORPHAN_SQL = """
SELECT r.rfq_number,
       r.id          AS rfq_id,
       i.line        AS line,
       i.id          AS item_id,
       '{col}'       AS col,
       s->>'name'        AS entry_name,
       s->>'supplier_id' AS orphan_id
FROM rfq_items i
JOIN rfqs r ON r.id = i.rfq_id
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(i.{col}, '[]'::jsonb)) AS s
WHERE s->>'supplier_id' IS NOT NULL
  AND s->>'supplier_id' <> ''
  AND NOT EXISTS (
      SELECT 1 FROM suppliers sp WHERE sp.id = CAST(s->>'supplier_id' AS uuid)
  )
"""


def _prod_url() -> str:
    url = None
    for line in open(".env"):
        if line.startswith("PROD_DATABASE_URL="):
            url = line.split("=", 1)[1].strip()
    if not url:
        sys.exit("PROD_DATABASE_URL not found in .env")
    # psycopg3 driver prefix — this venv has no psycopg2.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    args = ap.parse_args()

    engine = create_engine(_prod_url())

    with engine.connect() as c:
        orphans = []
        for col in COLUMNS:
            orphans += [dict(r._mapping) for r in c.execute(text(ORPHAN_SQL.format(col=col)))]

        if not orphans:
            print("No dangling supplier_id references found. Nothing to do.")
            return

        # Resolve each orphan by exact case-insensitive name match.
        plan, unresolved = [], []
        for o in orphans:
            cands = c.execute(text(
                "SELECT id, name, isinactive, use_instead FROM suppliers "
                "WHERE lower(name) = lower(:n) ORDER BY isinactive, id"
            ), {"n": o["entry_name"]}).fetchall()

            active = [x for x in cands if not x.isinactive and x.use_instead is None]
            # Only repoint when the choice is unambiguous: exactly one active
            # candidate, or exactly one candidate overall. Never guess between
            # two live suppliers with the same name.
            if len(active) == 1:
                pick = active[0]
            elif len(active) == 0 and len(cands) == 1:
                pick = cands[0]
            else:
                pick = None

            if pick is None:
                if len(cands) > 1:
                    why = f"{len(cands)} name candidates ({len(active)} active) — ambiguous"
                elif cands:
                    why = "only inactive/merged candidates"
                else:
                    why = "no name match"
                unresolved.append({**o, "reason": why})
            else:
                plan.append({**o, "new_id": str(pick.id), "new_name": pick.name})

        print(f"Found {len(orphans)} dangling ref(s) — {len(plan)} repairable, "
              f"{len(unresolved)} need manual review.\n")
        for p in plan:
            print(f"  {p['rfq_number']} line {p['line']:>3} [{p['col']}] "
                  f"{p['entry_name']!r}\n      {p['orphan_id']}  ->  {p['new_id']}")
        for u in unresolved:
            print(f"  !! {u['rfq_number']} line {u['line']} {u['entry_name']!r} "
                  f"({u['orphan_id']}) — {u['reason']}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to repair.")
            return

        # --- backup ---
        backup = {"created": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": []}
        for key in {(p["rfq_id"], p["line"], p["col"]) for p in plan}:
            rfq_id, line, col = key
            row = c.execute(text(
                f"SELECT suppliers, brand_suppliers FROM rfq_items "
                f"WHERE rfq_id = :r AND line = :l"
            ), {"r": rfq_id, "l": line}).fetchone()
            backup["rows"].append({
                "rfq_id": str(rfq_id), "line": line, "col": col,
                "before": row._mapping["suppliers"] if col == "suppliers"
                else row._mapping["brand_suppliers"],
            })
        with open(BACKUP_PATH, "w") as fh:
            json.dump(backup, fh, indent=2, default=str)
        print(f"\nbackup -> {BACKUP_PATH}")

    # --- apply (own transaction) ---
    with engine.begin() as c:
        fixed = 0
        per_rfq: dict[str, int] = {}
        for p in plan:
            col = p["col"]
            fixed += c.execute(text(f"""
                UPDATE rfq_items
                SET {col} = (
                    SELECT jsonb_agg(
                        CASE WHEN (elem->>'supplier_id') = :orphan
                             THEN jsonb_set(jsonb_set(elem, '{{supplier_id}}', to_jsonb(CAST(:new_id AS text))),
                                            '{{name}}', to_jsonb(CAST(:new_name AS text)))
                             ELSE elem
                        END
                        ORDER BY ord
                    )
                    FROM jsonb_array_elements({col}) WITH ORDINALITY AS t(elem, ord)
                )
                WHERE rfq_id = :rid AND line = :line
                  AND {col} @> CAST(:probe AS jsonb)
            """), {
                "orphan": p["orphan_id"], "new_id": p["new_id"], "new_name": p["new_name"],
                "rid": p["rfq_id"], "line": p["line"],
                "probe": json.dumps([{"supplier_id": p["orphan_id"], "name": p["entry_name"]}]),
            }).rowcount
            per_rfq[p["rfq_number"]] = per_rfq.get(p["rfq_number"], 0) + 1

        for rfq_number, n in sorted(per_rfq.items()):
            c.execute(text(
                "UPDATE rfqs SET history = COALESCE(history, '[]'::jsonb) || "
                "CAST(:e AS jsonb), updated_at = now() WHERE rfq_number = :n"
            ), {
                "e": json.dumps([{
                    "date": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "user": "system",
                    "action": (
                        f"Repaired {n} supplier link(s) that pointed at non-existent "
                        f"supplier records — remapped by exact supplier name."
                    ),
                }]),
                "n": rfq_number,
            })

    print(f"\nApplied: {fixed} reference(s) repointed across {len(per_rfq)} RFQ(s).")

    # --- verify ---
    with engine.connect() as c:
        remaining = 0
        for col in COLUMNS:
            remaining += c.execute(text(ORPHAN_SQL.format(col=col))).rowcount
        print(f"Remaining dangling refs: {remaining}")


if __name__ == "__main__":
    main()
