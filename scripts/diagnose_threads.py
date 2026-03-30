"""Diagnostic script: check thread and user state on production DB."""

import os
import sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

db_url = os.getenv("PROD_DATABASE_URL", "")
if not db_url:
    print("ERROR: PROD_DATABASE_URL not set in .env")
    sys.exit(1)

# Ensure sync driver compatible with psycopg (v3)
if "+asyncpg" in db_url:
    db_url = db_url.replace("postgresql+asyncpg", "postgresql+psycopg")
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(db_url)

with engine.connect() as conn:
    print("=" * 80)
    print("1. RECENT THREADS (last 15)")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT id, "userId", "userIdentifier", name, "createdAt", tags
        FROM threads
        ORDER BY "createdAt" DESC NULLS LAST
        LIMIT 15
    """)).fetchall()
    for r in rows:
        print(f"  id={r[0][:12]}...  userId={r[1]}  userIdent={r[2]}  name={r[3]!r:.40s}  created={r[4]}  tags={r[5]}")

    print()
    print("=" * 80)
    print("2. USERS TABLE")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT id, identifier, "createdAt"
        FROM users
        ORDER BY "createdAt" DESC NULLS LAST
    """)).fetchall()
    for r in rows:
        print(f"  id={r[0]}  identifier={r[1]}  created={r[2]}")

    print()
    print("=" * 80)
    print("3. THREADS WITH NULL userId (should be 0 for visible threads)")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE "userId" IS NULL) as null_user,
               COUNT(*) FILTER (WHERE "userId" IS NOT NULL) as has_user
        FROM threads
    """)).fetchall()
    for r in rows:
        print(f"  total={r[0]}  null_userId={r[1]}  has_userId={r[2]}")

    print()
    print("=" * 80)
    print("4. RECENT THREADS WITH NULL userId (the invisible ones)")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT t.id, t.name, t."createdAt",
               (SELECT COUNT(*) FROM steps s WHERE s."threadId" = t.id) as step_count
        FROM threads t
        WHERE t."userId" IS NULL
        ORDER BY t."createdAt" DESC NULLS LAST
        LIMIT 10
    """)).fetchall()
    for r in rows:
        print(f"  id={r[0][:12]}...  name={r[1]!r:.40s}  created={r[2]}  steps={r[3]}")

    print()
    print("=" * 80)
    print("5. RECENT STEPS (last 10) - check threadId linkage")
    print("=" * 80)
    rows = conn.execute(text("""
        SELECT s.id, s."threadId", s.type, s.name, s."createdAt"
        FROM steps s
        ORDER BY s."createdAt" DESC NULLS LAST
        LIMIT 10
    """)).fetchall()
    for r in rows:
        print(f"  step={r[0][:12]}...  thread={r[1][:12]}...  type={r[2]}  name={r[3]}  created={r[4]}")
