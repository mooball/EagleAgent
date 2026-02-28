"""Delete all checkpoint documents from Firestore.

This script removes all documents from the /checkpoints collection.
Use with caution - this cannot be undone!

Usage:
    uv run python clear_checkpoints.py
"""

import os
from dotenv import load_dotenv
from google.cloud import firestore

# Load environment variables
load_dotenv()

# Initialize Firestore client
client = firestore.Client(project="mooballai", database="(default)")
checkpoints_collection = client.collection("checkpoints")


def delete_checkpoints() -> int:
    """Delete all checkpoints using the correct FirestoreSaver structure.
    
    Structure is: /checkpoints/{thread_id}_{ns}/checkpoints/{checkpoint_id}
    Also handles "reference-only" documents (ghost placeholders).
    
    Returns:
        Total number of documents deleted
    """
    deleted_count = 0
    batch_size = 100
    
    # Get all partition documents using list_documents() (includes reference-only docs)
    partition_refs = list(checkpoints_collection.list_documents())
    
    if not partition_refs:
        print("No documents or references found.")
        return 0
    
    print(f"Found {len(partition_refs)} partition documents/references to process")
    
    for partition_ref in partition_refs:
        partition_id = partition_ref.id
        print(f"\nProcessing: {partition_id}")
        
        # Check if document actually exists or is just a reference
        partition_doc = partition_ref.get()
        if partition_doc.exists:
            print(f"  ✓ Document exists with data")
        else:
            print(f"  ⚠ Reference-only (no data, but may have subcollections)")
        
        # Get the checkpoints subcollection for this partition
        checkpoints_subcollection = partition_ref.collection("checkpoints")
        
        # Delete all checkpoint documents in this subcollection
        sub_deleted = 0
        while True:
            docs = list(checkpoints_subcollection.limit(batch_size).stream())
            if not docs:
                break
            
            batch = client.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
            
            sub_deleted += len(docs)
            print(f"  Deleted {len(docs)} checkpoint documents (subtotal: {sub_deleted})")
        
        if sub_deleted == 0:
            print(f"  No checkpoints in subcollection")
        
        # Delete the partition document itself (works for both real docs and references)
        try:
            partition_ref.delete()
            deleted_count += 1
            print(f"  ✓ Deleted partition: {partition_id}")
        except Exception as e:
            print(f"  ✗ Error deleting partition: {e}")
        
        deleted_count += sub_deleted
    
    return deleted_count


def main():
    """Main entry point."""
    print("Counting checkpoint documents...")
    
    # Count partition documents
    partition_refs = list(checkpoints_collection.list_documents())
    partition_count = len(partition_refs)
    
    if partition_count == 0:
        print("No checkpoint documents found. Nothing to delete.")
        return
    
    # Sample checkpoint count from first partition
    checkpoint_count = 0
    if partition_refs:
        first_partition = partition_refs[0]
        checkpoints_sub = first_partition.collection("checkpoints")
        checkpoint_count = len(list(checkpoints_sub.limit(100).stream()))
    
    print(f"\nFound {partition_count} partition(s) (threads/conversations)")
    if checkpoint_count > 0:
        print(f"Sample: First partition has {checkpoint_count} checkpoint document(s)")
    
    # Confirm deletion
    response = input("\nAre you sure you want to delete ALL checkpoints? This cannot be undone! (yes/no): ")
    
    if response.lower() != "yes":
        print("Deletion cancelled.")
        return
    
    print("\nDeleting checkpoints...")
    total_deleted = delete_checkpoints()
    
    print(f"\n✓ Successfully deleted {total_deleted} total documents (partitions + checkpoints).")


if __name__ == "__main__":
    main()
