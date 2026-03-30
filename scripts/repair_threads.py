"""Repair orphaned threads on production.

Finds threads with NULL userId that have steps created by known users
and backfills userId, userIdentifier, name, and tags.
"""

import os
import sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

db_url = os.getenv("PROD_DATABASE_URL", "")
if not db_url:
    print("ERROR: PROD_DATABASE_URL not set in .env")
    sys.exit(1)

if "+asyncpg" in db_url:
    db_url = db_url.replace("postgresql+asyncpg", "postgresql+psycopg")
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

DRY_RUN = "--apply" not in sys.argv

engine = create_engine(db_url)

with engine.connect() as conn:
    # Build a map of userIdentifier → userId from the users table
    users = conn.execute(text('SELECT id, identifier FROM users')).fetchall()
    user_map = {row[1]: row[0] for row in users}
    print(f"Known users: {list(user_map.keys())}")

    # Find orphaned threads (NULL userId) that have user_message steps
    orphans = conn.execute(text("""
        SELECT t.id,
               (SELECT s.name FROM steps s
                WHERE s."threadId" = t.id AND s.type = 'user_message'
                LIMIT 1) AS user_identifier,
               (SELECT s2.output FROM steps s2
                WHERE s2."threadId" = t.id AND s2.type = 'user_message'
                ORDER BY s2."createdAt" ASC LIMIT 1) AS first_message
        FROM threads t
        WHERE t."userId" IS NULL
        ORDER BY t."createdAt" DESC
    """)).fetchall()

    if not orphans:
        print("\nNo orphaned threads found. All good!")
        sys.exit(0)

    print(f"\nFound {len(orphans)} orphaned thread(s):\n")

    fixed = 0
    for thread_id, user_identifier, first_message in orphans:
        user_id = user_map.get(user_identifier)
        thread_name = (first_message or "")[:100] if first_message else None

        status = "FIXABLE" if user_id else "SKIP (unknown user)"
        print(f"  thread={thread_id[:12]}...  user={user_identifier}  "
              f"name={thread_name!r:.50s}  → {status}")

        if user_id and not DRY_RUN:
            conn.execute(text("""
                UPDATE threads
                SET "userId" = :user_id,
                    "userIdentifier" = :user_identifier,
                    "name" = COALESCE(:name, "name"),
                    "tags" = COALESCE(:tags, "tags")
                WHERE id = :thread_id AND "userId" IS NULL
            """), {
                "thread_id": thread_id,
                "user_id": user_id,
                "user_identifier": user_identifier,
                "name": thread_name,
                "tags": "EagleAgent",
                })
            fixed += 1

    if DRY_RUN:
        print(f"\n⚠️  DRY RUN — no changes made. Run with --apply to fix.")
    else:
        conn.commit()
        print(f"\n✅ Fixed {fixed} thread(s).")
