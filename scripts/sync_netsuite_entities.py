"""
sync_netsuite_entities.py

Runs all NetSuite entity syncs in dependency order, inline (no subprocess).
Designed to be called from the background sync loop in main.py.

Usage:
  from scripts.sync_netsuite_entities import run_netsuite_entity_syncs
  run_netsuite_entity_syncs()
"""

import importlib
import logging
import sys
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Entity syncs in dependency order (brands first, then everything else)
STEPS: list[dict] = [
    {"name": "brands",        "module": "scripts.sync_netsuite_brands",        "args": ["--resume"],  "description": "Sync brands"},
    {"name": "products",      "module": "scripts.sync_netsuite_products",      "args": ["--resume"],  "description": "Sync products"},
    {"name": "suppliers",     "module": "scripts.sync_netsuite_suppliers",     "args": ["--resume"],  "description": "Sync suppliers"},
    {"name": "quotes",        "module": "scripts.sync_netsuite_quotes",        "args": ["--resume"],  "description": "Sync quotes"},
    {"name": "sales_orders",  "module": "scripts.sync_netsuite_sales_orders",  "args": ["--resume"],  "description": "Sync sales orders"},
    {"name": "contacts",      "module": "scripts.sync_netsuite_contacts",      "args": ["--resume"],  "description": "Sync contacts"},
    {"name": "customers",     "module": "scripts.sync_netsuite_customers",     "args": ["--resume"],  "description": "Sync customers"},
    {"name": "opportunities", "module": "scripts.sync_netsuite_opportunities", "args": ["--resume"],  "description": "Sync opportunities"},
    {"name": "rfq_opp_links",  "module": "scripts.backfill_rfq_opp_links",     "args": [],              "description": "Link RFQs to opportunities"},
]


def run_netsuite_entity_syncs() -> dict[str, bool]:
    """Run all 8 NetSuite entity syncs sequentially, inline.

    Each step calls the sync module's main() function directly by temporarily
    setting sys.argv to pass the required arguments (e.g. --resume).

    Returns a dict mapping step name → success (True/False).
    """
    original_argv = sys.argv[:]
    results: dict[str, bool] = {}

    for step in STEPS:
        name = step["name"]
        module_name = step["module"]
        args = step["args"]
        start = time.time()

        # Set up argv as if called from command line
        sys.argv = [module_name] + args

        try:
            logger.info(f"[netsuite-sync] Starting: {step['description']}")
            mod = importlib.import_module(module_name)
            mod.main()
            elapsed = time.time() - start
            logger.info(f"[netsuite-sync] ✓ {name} completed in {elapsed:.1f}s")
            results[name] = True
        except SystemExit as e:
            elapsed = time.time() - start
            if e.code == 0 or e.code is None:
                logger.info(f"[netsuite-sync] ✓ {name} completed in {elapsed:.1f}s")
                results[name] = True
            else:
                logger.error(f"[netsuite-sync] ✗ {name} exited with code {e.code} after {elapsed:.1f}s")
                results[name] = False
        except Exception:
            elapsed = time.time() - start
            logger.exception(f"[netsuite-sync] ✗ {name} failed after {elapsed:.1f}s")
            results[name] = False
        finally:
            sys.argv = original_argv

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = run_netsuite_entity_syncs()
    passed = sum(1 for v in results.values() if v)
    failed = len(results) - passed
    print(f"\nEntity sync complete: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
