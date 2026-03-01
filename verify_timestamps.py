"""Verify that timestamps are being added to checkpoint documents.

This checks if created_at field exists in checkpoint documents.

Usage:
    uv run python verify_timestamps.py
"""

import os
from dotenv import load_dotenv
from google.cloud import firestore

load_dotenv()

client = firestore.Client(project=os.getenv("GOOGLE_PROJECT_ID"), database="(default)")
checkpoints_ref = client.collection("checkpoints")


def check_timestamps():
    """Check all checkpoint documents for created_at timestamps."""
    
    partition_refs = list(checkpoints_ref.list_documents())
    
    if not partition_refs:
        print("No partition documents found.")
        return
    
    print(f"Checking {len(partition_refs)} partition(s) for timestamps...\n")
    
    found_timestamps = 0
    missing_timestamps = 0
    
    for partition_ref in partition_refs:
        partition_id = partition_ref.id
        
        # Check if partition document itself has timestamp
        partition_doc = partition_ref.get()
        if partition_doc.exists:
            partition_data = partition_doc.to_dict()
            if partition_data and "created_at" in partition_data:
                print(f"\nPartition: {partition_id}")
                print(f"  ✓ Partition document has timestamp: {partition_data['created_at']}")
            else:
                print(f"\nPartition: {partition_id}")
                print(f"  ✗ Partition document MISSING timestamp")
        else:
            print(f"\nPartition: {partition_id} (reference only)")
        
        # Get the checkpoints subcollection - use list_documents() to get all refs
        checkpoints_sub = partition_ref.collection("checkpoints")
        checkpoint_refs = list(checkpoints_sub.list_documents())
        
        if not checkpoint_refs:
            print(f"  No checkpoint document references")
            continue
        
        print(f"  Found {len(checkpoint_refs)} checkpoint reference(s)")
        
        # Get actual documents from references
        checkpoint_docs = [ref.get() for ref in checkpoint_refs if ref.get().exists]
        
        if not checkpoint_docs:
            print(f"  (All checkpoint references are empty)")
            continue
        
        print(f"  Checking {len(checkpoint_docs)} actual checkpoint document(s):")
        
        for checkpoint_doc in checkpoint_docs:
            doc_id = checkpoint_doc.id
            data = checkpoint_doc.to_dict()
            
            if data and "created_at" in data:
                timestamp = data["created_at"]
                print(f"    ✓ {doc_id[:20]}... has timestamp: {timestamp}")
                found_timestamps += 1
            else:
                print(f"    ✗ {doc_id[:20]}... MISSING timestamp")
                # Show what fields it does have
                if data:
                    print(f"      Available fields: {list(data.keys())[:5]}")
                missing_timestamps += 1
    
    print(f"\n{'=' * 60}")
    print(f"Summary:")
    print(f"  ✓ With timestamps: {found_timestamps}")
    print(f"  ✗ Missing timestamps: {missing_timestamps}")
    print(f"{'=' * 60}")
    
    if missing_timestamps > 0:
        print("\nTimestamps are NOT being added correctly.")
        print("Possible issues:")
        print("1. The app hasn't been restarted since code changes")
        print("2. The aput/put methods aren't being called")
        print("3. An exception is occurring silently")
    else:
        print("\n✓ All checkpoints have timestamps!")


if __name__ == "__main__":
    check_timestamps()
