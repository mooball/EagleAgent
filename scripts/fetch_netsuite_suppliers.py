"""
Fetch NetSuite vendor records updated since a given date and save to JSON.

Usage:
    uv run python scripts/fetch_netsuite_suppliers.py --since 2026-04-01
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from includes.netsuite import NetSuiteClient
from includes.netsuite.queries import suppliers_updated_since
from config.settings import Config


def main():
    parser = argparse.ArgumentParser(description="Fetch vendors from NetSuite")
    parser.add_argument(
        "--since",
        default=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
        help="Fetch vendors modified on or after this date (YYYY-MM-DD). Default: 30 days ago.",
    )
    args = parser.parse_args()

    print(f"Fetching vendors updated since {args.since}...")
    client = NetSuiteClient()
    query = suppliers_updated_since(args.since)
    print(f"Query: {query}")
    print()

    rows = client.suiteql(query)
    print(f"Fetched {len(rows)} vendor records")

    # Save to JSON
    output_path = Path(Config.DATA_DIR) / "netsuite_vendors.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"Saved to {output_path}")

    # Print first few as a preview
    if rows:
        print(f"\nFirst 10 records:")
        for row in rows[:10]:
            # Strip the 'links' key that NetSuite adds
            display = {k: v for k, v in row.items() if k != "links"}
            print(f"  {display}")


if __name__ == "__main__":
    main()
