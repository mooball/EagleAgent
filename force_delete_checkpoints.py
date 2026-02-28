"""Force delete all checkpoints by directly addressing each level.

This script uses a brute-force approach to delete everything.

Usage:
    uv run python force_delete_checkpoints.py
"""

import os
from dotenv import load_dotenv
from google.cloud import firestore

# Load environment variables
load_dotenv()

# Initialize Firestore client
client = firestore.Client(project="mooballai", database="(default)")


def delete_everything_in_checkpoints():
    """Forcefully delete all data in /checkpoints collection."""
    
    checkpoints_ref = client.collection("checkpoints")
    
    # Get all document references (including empty ones)
    all_refs = list(checkpoints_ref.list_documents())
    
    if not all_refs:
        print("No documents found.")
        return 0
    
    print(f"Found {len(all_refs)} document references\n")
    
    total_deleted = 0
    
    for doc_ref in all_refs:
        doc_id = doc_ref.id
        print(f"Processing: {doc_id}")
        
        # Get all subcollections
        all_subcollections = list(doc_ref.collections())
        
        if all_subcollections:
            print(f"  Found {len(all_subcollections)} subcollection(s): {[s.id for s in all_subcollections]}")
            
            # Delete each subcollection
            for subcol in all_subcollections:
                print(f"    Deleting subcollection: {subcol.id}")
                sub_count = delete_collection(subcol)
                print(f"      Deleted {sub_count} documents from {subcol.id}")
                total_deleted += sub_count
        
        # Try to delete the document itself
        try:
            doc_ref.delete()
            print(f"  ✓ Deleted document: {doc_id}")
            total_deleted += 1
        except Exception as e:
            print(f"  ✗ Failed to delete {doc_id}: {e}")
        
        print()
    
    return total_deleted


def delete_collection(coll_ref, batch_size=500):
    """Delete all documents in a collection."""
    deleted = 0
    
    while True:
        docs = list(coll_ref.limit(batch_size).stream())
        if not docs:
            break
        
        # Delete in batch
        batch = client.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        
        deleted += len(docs)
    
    return deleted


if __name__ == "__main__":
    print("=" * 60)
    print("FORCE DELETE ALL CHECKPOINTS")
    print("=" * 60)
    print()
    
    response = input("This will DELETE EVERYTHING in /checkpoints. Type 'DELETE' to confirm: ")
    
    if response != "DELETE":
        print("Cancelled.")
    else:
        print("\nDeleting...\n")
        total = delete_everything_in_checkpoints()
        print(f"\n{'=' * 60}")
        print(f"Total deleted: {total} documents")
        print(f"{'=' * 60}")
