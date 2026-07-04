"""
nightly_sync.py

Orchestrates nightly post-processing tasks after NetSuite entity syncs have
already run (via the background NETSUITE_SYNC_ENABLED loop in main.py).

Steps:
  1. Link Supplier Brands (--since 2d — post-sync linking)
  2. Categorize Suppliers (--limit 100 — batch of uncategorized)
  3. Generate Supplier Notes (--limit 100 — research missing notes)
  4. Update Supplier Embeddings (re-embed any with NULL embedding)

Usage:
  uv run python -m scripts.nightly_sync
  uv run python -m scripts.nightly_sync --dry-run
  uv run python -m scripts.nightly_sync --step link_supplier_brands
"""

import argparse
import base64
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime


def ensure_google_credentials():
    """Decode GOOGLE_SERVICE_ACCOUNT_BASE64 to a temp file if needed.

    Railway cron jobs don't go through start.sh, so the service account
    JSON may not be written to disk yet.
    """
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds_path and os.path.isfile(creds_path):
        return  # Already available

    b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_BASE64", "")
    if not b64:
        print("WARNING: No GOOGLE_SERVICE_ACCOUNT_BASE64 — embedding steps may fail")
        return

    creds_file = "/tmp/service-account-key.json"
    with open(creds_file, "wb") as f:
        f.write(base64.b64decode(b64))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file
    print(f"✅ Decoded service account credentials to {creds_file}")

STEPS = [
    {
        "name": "link_supplier_brands",
        "module": "scripts.link_supplier_brands",
        "args": ["--since", "2d"],
        "description": "Link suppliers to brands from recent transactions",
    },
    {
        "name": "categorize_suppliers",
        "module": "scripts.categorize_suppliers_job",
        "args": ["--limit", "100"],
        "description": "Categorize uncategorized suppliers (batch of 100)",
    },
    {
        "name": "generate_supplier_notes",
        "module": "scripts.generate_supplier_notes",
        "args": ["--limit", "100"],
        "description": "Research and generate notes for suppliers missing them (batch of 100)",
    },
    {
        "name": "update_supplier_embeddings",
        "module": "scripts.update_supplier_embeddings",
        "args": [],
        "description": "Regenerate embeddings for suppliers with updated notes",
    },
]


def run_step(step: dict, dry_run: bool = False) -> bool:
    """Run a single sync step. Returns True on success, False on failure."""
    cmd = [sys.executable, "-m", step["module"]] + step["args"]
    if dry_run:
        cmd.append("--dry-run")

    print(f"\n{'='*60}")
    print(f"  {step['description']}")
    print(f"  Command: {' '.join(cmd)}")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}\n")

    start = time.time()
    result = subprocess.run(cmd)
    elapsed = time.time() - start

    minutes, seconds = divmod(int(elapsed), 60)
    time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    if result.returncode == 0:
        print(f"\n  ✓ {step['name']} completed in {time_str}")
        return True
    else:
        print(f"\n  ✗ {step['name']} FAILED (exit code {result.returncode}) after {time_str}")
        return False


def main():
    ensure_google_credentials()

    parser = argparse.ArgumentParser(description="Nightly NetSuite sync")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to each sync script",
    )
    parser.add_argument(
        "--step",
        choices=[s["name"] for s in STEPS],
        help="Run only a single step",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue running remaining steps if one fails",
    )
    args = parser.parse_args()

    steps = STEPS
    if args.step:
        steps = [s for s in STEPS if s["name"] == args.step]

    print(f"Nightly Post-Processing Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print("DRY RUN — no changes will be written")
    print(f"Steps: {', '.join(s['name'] for s in steps)}")

    total_start = time.time()
    results = {}

    for step in steps:
        success = run_step(step, dry_run=args.dry_run)
        results[step["name"]] = success
        if not success and not args.continue_on_error:
            print(f"\nAborting — {step['name']} failed. Use --continue-on-error to keep going.")
            break

    # Summary
    elapsed = time.time() - total_start
    minutes, seconds = divmod(int(elapsed), 60)
    time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    print(f"\n{'='*60}")
    print(f"  SUMMARY — Total time: {time_str}")
    print(f"{'='*60}")
    for name, success in results.items():
        status = "✓" if success else "✗ FAILED"
        print(f"  {status}  {name}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} step(s) failed: {', '.join(failed)}")
        sys.stdout.flush()
        os._exit(1)
    else:
        print(f"\nAll {len(results)} steps completed successfully.")
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
