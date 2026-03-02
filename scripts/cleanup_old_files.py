#!/usr/bin/env python3
"""
Cleanup script for expired file attachments.

Removes files from GCS bucket and updates database records for files
older than the specified TTL (default: 30 days).

Can be run manually or scheduled as a cron job.
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from includes.storage_utils import delete_file_from_gcs

# Load environment variables
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def cleanup_expired_files(days: int = 30, dry_run: bool = False):
    """
    Clean up files older than specified days.
    
    Args:
        days: Number of days to keep files (default: 30)
        dry_run: If True, only report what would be deleted without deleting
    """
    database_url = os.getenv("DATABASE_URL")
    bucket_name = os.getenv("GCP_BUCKET_NAME")
    
    if not database_url:
        logger.error("DATABASE_URL not set in environment")
        return
    
    if not bucket_name:
        logger.warning("GCP_BUCKET_NAME not set - will only clean database records")
    
    # Calculate cutoff date
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    logger.info(f"Cleaning up files older than {cutoff_date.isoformat()}")
    
    # Connect to database
    # Convert aiosqlite URL to sync sqlite for this script
    if "aiosqlite" in database_url:
        database_url = database_url.replace("sqlite+aiosqlite", "sqlite")
    
    engine = create_engine(database_url)
    
    try:
        with engine.connect() as conn:
            # Query for expired files
            query = text("""
                SELECT id, name, objectKey, threadId, createdAt
                FROM elements
                WHERE createdAt < :cutoff_date
                AND objectKey IS NOT NULL
                ORDER BY createdAt ASC
            """)
            
            result = conn.execute(query, {"cutoff_date": cutoff_date.isoformat()})
            expired_files = result.fetchall()
            
            logger.info(f"Found {len(expired_files)} expired files")
            
            deleted_count = 0
            failed_count = 0
            
            for file_record in expired_files:
                file_id = file_record[0]
                file_name = file_record[1]
                object_key = file_record[2]
                thread_id = file_record[3]
                created_at = file_record[4]
                
                logger.info(f"Processing: {file_name} (created: {created_at}, thread: {thread_id})")
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would delete from GCS: {object_key}")
                    deleted_count += 1
                    continue
                
                # Delete from GCS
                if bucket_name and object_key:
                    success = delete_file_from_gcs(bucket_name, object_key)
                    if success:
                        logger.info(f"Deleted from GCS: {object_key}")
                    else:
                        logger.warning(f"Failed to delete from GCS: {object_key}")
                        failed_count += 1
                        continue
                
                # Mark as deleted in database (or remove record)
                # Option 1: Delete the record
                delete_query = text("DELETE FROM elements WHERE id = :file_id")
                conn.execute(delete_query, {"file_id": file_id})
                conn.commit()
                
                deleted_count += 1
                logger.info(f"Deleted database record for: {file_name}")
            
            logger.info(f"""
Cleanup complete:
- Total expired files: {len(expired_files)}
- Successfully deleted: {deleted_count}
- Failed: {failed_count}
- TTL: {days} days
- Cutoff date: {cutoff_date.isoformat()}
            """)
            
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise
    finally:
        engine.dispose()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Clean up expired file attachments")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to keep files (default: 30)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without actually deleting"
    )
    
    args = parser.parse_args()
    
    logger.info(f"Starting cleanup (days={args.days}, dry_run={args.dry_run})")
    cleanup_expired_files(days=args.days, dry_run=args.dry_run)
    logger.info("Cleanup script finished")
