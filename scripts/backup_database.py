"""
backup_database.py

Nightly PostgreSQL backup to Backblaze B2 (S3-compatible).

Naming convention:
  - 1st of month: monthly_{month}_database.sql.gz  (12 rotating monthly backups)
  - Other days:   daily_{dow}_database.sql.gz       (7 rotating daily backups)

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

import gzip
import os
import subprocess
import shutil
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
    """Run pg_dump against the given database URL, compressed with gzip.
    
    Tries the pg_dump binary first; if unavailable, falls back to psycopg's
    copy command for a pure-Python dump.
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    db = parsed.path.lstrip("/")

    # Try pg_dump binary first (faster, full-fidelity dump)
    if shutil.which("pg_dump"):
        env = os.environ.copy()
        env["PGPASSWORD"] = parsed.password or ""
        cmd = [
            "pg_dump",
            "-h", host,
            "-p", str(port),
            "-U", parsed.username or "postgres",
            "-d", db,
            "--no-owner",
            "--no-acl",
        ]
        print(f"  Running pg_dump on {host}:{port}/{db}...")
        with open(output_path, "wb") as f:
            # Try piping through gzip if available
            if shutil.which("gzip"):
                dump_proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                gzip_proc = subprocess.Popen(["gzip"], stdin=dump_proc.stdout, stdout=f, stderr=subprocess.PIPE)
                dump_proc.stdout.close()
                gzip_proc.wait()
                dump_proc.wait()
            else:
                # Use Python gzip
                dump_proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with gzip.open(f, "wb") as gz:
                    while chunk := dump_proc.stdout.read(1024 * 1024):
                        gz.write(chunk)
                dump_proc.wait()

        if dump_proc.returncode != 0:
            stderr = dump_proc.stderr.read().decode() if dump_proc.stderr else ""
            print(f"  ERROR: pg_dump failed:\n{stderr}", file=sys.stderr)
            sys.exit(1)
    else:
        # Fallback: pure-Python dump using psycopg COPY
        print(f"  pg_dump not found, using psycopg COPY fallback on {host}:{port}/{db}...")
        _psycopg_dump(database_url, output_path)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Dump complete: {size_mb:.1f} MB")


def _psycopg_dump(database_url: str, output_path: str) -> None:
    """Pure-Python database dump using psycopg COPY TO for each table."""
    import psycopg

    # Ensure we use a sync psycopg connection URL
    conninfo = database_url.replace("postgresql+psycopg://", "postgresql://")
    conninfo = conninfo.replace("postgresql+asyncpg://", "postgresql://")

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            # Get all user tables
            cur.execute("""
                SELECT schemaname, tablename 
                FROM pg_tables 
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY schemaname, tablename
            """)
            tables = cur.fetchall()

            with gzip.open(output_path, "wt", encoding="utf-8") as gz:
                gz.write(f"-- Database dump via psycopg COPY\n")
                gz.write(f"-- Date: {datetime.now().isoformat()}\n\n")

                for schema, table in tables:
                    qualified = f'"{schema}"."{table}"' if schema != "public" else f'"{table}"'
                    gz.write(f"-- Table: {qualified}\n")

                    # Get column info for COPY header
                    cur.execute(f"SELECT * FROM {qualified} LIMIT 0")
                    columns = [desc.name for desc in cur.description]
                    col_list = ", ".join(f'"{c}"' for c in columns)

                    gz.write(f"COPY {qualified} ({col_list}) FROM stdin;\n")

                    # COPY TO stdout
                    with cur.copy(f"COPY {qualified} TO STDOUT") as copy:
                        for row in copy:
                            gz.write(row.decode("utf-8") if isinstance(row, bytes) else row)

                    gz.write("\\.\n\n")


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
