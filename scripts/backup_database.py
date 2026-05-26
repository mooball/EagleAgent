"""
backup_database.py

Nightly PostgreSQL backup to Backblaze B2 (S3-compatible).

Naming convention:
  - 1st of month: monthly_{month}_database.sql  (12 rotating monthly backups)
  - Other days:   daily_{dow}_database.sql       (7 rotating daily backups)

Environment variables required:
  DATABASE_URL    - PostgreSQL connection URL (from app config)
  B2_KEY_ID      - Backblaze B2 application key ID
  B2_APP_KEY     - Backblaze B2 application key
  B2_ENDPOINT    - Backblaze B2 S3 endpoint (e.g. s3.us-west-004.backblazeb2.com)
  B2_BUCKET      - Backblaze B2 bucket name

Usage:
  uv run python -m scripts.backup_database
  uv run python -m scripts.backup_database --dry-run
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from urllib.parse import urlparse

import boto3
from dotenv import load_dotenv

load_dotenv()


def get_backup_filename() -> str:
    """Generate backup filename based on current date."""
    now = datetime.now()
    if now.day == 1:
        month_name = now.strftime("%B").lower()
        return f"monthly_{month_name}_database.sql.gz"
    else:
        dow = now.strftime("%A").lower()
        return f"daily_{dow}_database.sql.gz"


def pg_dump(database_url: str, output_path: str) -> None:
    """Run pg_dump against the given database URL, compressed with gzip."""
    parsed = urlparse(database_url)

    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""

    cmd = [
        "pg_dump",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        "-d", parsed.path.lstrip("/"),
        "--no-owner",
        "--no-acl",
    ]

    print(f"  Running pg_dump on {parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}...")
    # Pipe through gzip for compression
    with open(output_path, "wb") as f:
        dump_proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gzip_proc = subprocess.Popen(["gzip"], stdin=dump_proc.stdout, stdout=f, stderr=subprocess.PIPE)
        dump_proc.stdout.close()  # Allow dump_proc to receive SIGPIPE
        gzip_proc.wait()
        dump_proc.wait()

    if dump_proc.returncode != 0:
        stderr = dump_proc.stderr.read().decode() if dump_proc.stderr else ""
        print(f"  ERROR: pg_dump failed:\n{stderr}", file=sys.stderr)
        sys.exit(1)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Dump complete: {size_mb:.1f} MB")


def upload_to_b2(filepath: str, filename: str, dry_run: bool = False) -> None:
    """Upload a file to Backblaze B2 via S3-compatible API."""
    key_id = os.getenv("B2_KEY_ID")
    app_key = os.getenv("B2_APP_KEY")
    endpoint = os.getenv("B2_ENDPOINT")
    bucket = os.getenv("B2_BUCKET")

    if not all([key_id, app_key, endpoint, bucket]):
        print("  ERROR: Missing B2 credentials. Set B2_KEY_ID, B2_APP_KEY, B2_ENDPOINT, B2_BUCKET.", file=sys.stderr)
        sys.exit(1)

    endpoint_url = f"https://{endpoint}"

    if dry_run:
        print(f"  [DRY RUN] Would upload {filepath} → s3://{bucket}/{filename}")
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=key_id,
        aws_secret_access_key=app_key,
    )

    print(f"  Uploading to s3://{bucket}/{filename}...")
    s3.upload_file(filepath, bucket, filename)
    print("  Upload complete.")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backup PostgreSQL to Backblaze B2")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without executing")
    args = parser.parse_args()

    from config import config
    db_url = config.DATABASE_URL
    if not db_url:
        print("ERROR: DATABASE_URL not set. Check your .env settings.", file=sys.stderr)
        sys.exit(1)

    # Strip async driver prefix if present (pg_dump needs raw postgres URL)
    db_url = db_url.replace("postgresql+psycopg://", "postgresql://")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    filename = get_backup_filename()
    print(f"Backup: {filename}")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    if args.dry_run:
        print(f"  [DRY RUN] Would dump database and upload as {filename}")
        return

    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=True) as tmp:
        tmp_path = tmp.name

    # pg_dump writes to tmp_path
    pg_dump(db_url, tmp_path)

    try:
        upload_to_b2(tmp_path, filename)
    finally:
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print("Done.")


if __name__ == "__main__":
    main()
