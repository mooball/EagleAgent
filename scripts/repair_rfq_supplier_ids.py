#!/usr/bin/env python
"""Repair non-UUID supplier_id values in rfq_items.suppliers by name-matching.

Background (2026-09-07): the agent invented supplier ids ("sup_1597", "926")
when composing add_suppliers_bulk calls, and they were persisted verbatim.
Downstream renderers feed supplier_id into UUID columns and crash. The write
path now strips such ids; this script repairs the rows already in the DB.

Usage (read-only dry run first):
    uv run python scripts/repair_rfq_supplier_ids.py
    uv run python scripts/repair_rfq_supplier_ids.py --rfq RFQ-2026-1829
    uv run python scripts/repair_rfq_supplier_ids.py --apply          # writes
    uv run python scripts/repair_rfq_supplier_ids.py --database-url URL [--apply]
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

# Allow `uv run python scripts/...` to import the app package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, attributes, sessionmaker


def _is_valid_uuid(value: object) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _load_env() -> None:
    """Parse .env into os.environ (repo convention — .env is not auto-loaded)."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _db_url(args: argparse.Namespace) -> str:
    _load_env()
    url = args.database_url or os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("No database URL — set PROD_DATABASE_URL in .env or pass --database-url.")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="Override the database URL (defaults to PROD_DATABASE_URL).")
    parser.add_argument("--rfq", help="Only repair this RFQ number (e.g. RFQ-2026-1829).")
    parser.add_argument("--apply", action="store_true", help="Write repairs (default is dry run).")
    args = parser.parse_args()

    from includes.dashboard.models import RFQ, RFQItem, Supplier

    engine = create_engine(_db_url(args))
    SessionLocal = sessionmaker(bind=engine)
    session: Session = SessionLocal()

    try:
        q = session.query(RFQItem).filter(RFQItem.suppliers.isnot(None))
        if args.rfq:
            rfq_row = session.query(RFQ).filter(RFQ.rfq_number == args.rfq).first()
            if not rfq_row:
                sys.exit(f"RFQ '{args.rfq}' not found.")
            q = q.filter(RFQItem.rfq_id == rfq_row.id)

        items = q.all()
        rfq_numbers = {
            r.id: r.rfq_number
            for r in session.query(RFQ).filter(RFQ.id.in_({i.rfq_id for i in items})).all()
        }

        # Index suppliers by lowercase name (one name -> one live supplier).
        names_needed = {
            (sup.get("name") or "").strip().lower()
            for item in items
            for sup in (item.suppliers or [])
            if isinstance(sup, dict)
            and sup.get("supplier_id")
            and not _is_valid_uuid(sup["supplier_id"])
        }
        name_to_supplier: dict[str, tuple[str, str]] = {}
        if names_needed:
            rows = (
                session.query(Supplier)
                .filter(func.lower(Supplier.name).in_(names_needed),
                        Supplier.isinactive == False)
                .all()
            )
            for s in rows:
                key = s.name.strip().lower()
                existing = name_to_supplier.get(key)
                if existing is None or (s.use_instead is None and existing[2] is not None):
                    name_to_supplier[key] = (str(s.id), s.name, s.use_instead)

        report: list[tuple] = []  # (rfq, line, name, old_id, new_id, action)
        changes_by_rfq: dict[str, int] = {}

        for item in items:
            changed = False
            for sup in item.suppliers or []:
                if not isinstance(sup, dict):
                    continue
                sid = sup.get("supplier_id")
                if not sid or _is_valid_uuid(sid):
                    continue
                name = (sup.get("name") or "").strip()
                match = name_to_supplier.get(name.lower())
                if match is None:
                    report.append((rfq_numbers[item.rfq_id], item.line, name, str(sid), "", "SKIP: no exact-name match"))
                    continue
                new_id, _, _ = match
                report.append((rfq_numbers[item.rfq_id], item.line, name, str(sid), new_id, "REPAIR"))
                if args.apply:
                    sup["supplier_id"] = new_id
                    changed = True
                    changes_by_rfq[rfq_numbers[item.rfq_id]] = changes_by_rfq.get(rfq_numbers[item.rfq_id], 0) + 1
            if changed and args.apply:
                item.suppliers = list(item.suppliers)
                attributes.flag_modified(item, "suppliers")

        if args.apply and changes_by_rfq:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for rfq_number, count in changes_by_rfq.items():
                rfq_row = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
                if not rfq_row:
                    continue
                history = list(rfq_row.history or [])
                history.append({
                    "date": now,
                    "user": "system",
                    "action": f"Repaired {count} supplier id(s) on line items (invalid id → matched by name)",
                })
                rfq_row.history = history
                rfq_row.updated_at = datetime.now(timezone.utc)
            session.commit()
            print(f"APPLIED: repaired ids on {len(changes_by_rfq)} RFQ(s), "
                  f"{sum(changes_by_rfq.values())} entries total.")
        elif not args.apply:
            print("DRY RUN — no writes. Re-run with --apply to repair.")
            print(f"{'RFQ':<16} {'line':>4}  {'supplier':<38} {'old id':<12} {'action'}")
            for rfq, line, name, old_id, new_id, action in report:
                print(f"{rfq:<16} {line:>4}  {name[:36]:<38} {old_id:<12} {action}")

        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
