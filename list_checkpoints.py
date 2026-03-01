"""List all checkpoint documents from Firestore.

This script lists all documents in the /checkpoints collection
to help understand the structure.

Usage:
    uv run python list_checkpoints.py
"""

import os
from dotenv import load_dotenv
from google.cloud import firestore

# Load environment variables
load_dotenv()

# Initialize Firestore client
client = firestore.Client(project=os.getenv("GOOGLE_PROJECT_ID"), database="(default)")
checkpoints_collection = client.collection("checkpoints")


def list_documents():
    """List all top-level documents in the checkpoints collection."""
    print(f"Project: {os.getenv('GOOGLE_PROJECT_ID')}")
    print(f"Database: (default)")
    print(f"Collection path: checkpoints")
    print(f"Client info: {client.project}\n")
    
    print("Querying documents in /checkpoints collection...\n")
    
    # Try multiple approaches to list documents
    docs = []
    doc_refs = []
    
    # Approach 1: Using stream()
    print("=== Method 1: Using stream() ===")
    try:
        docs = list(checkpoints_collection.stream())
        print(f"Found {len(docs)} documents via stream()")
        for doc in docs:
            print(f"  - {doc.id}")
    except Exception as e:
        print(f"Error with stream(): {e}")
    
    print()
    
    # Approach 2: Using list_documents()
    print("=== Method 2: Using list_documents() ===")
    try:
        doc_refs = list(checkpoints_collection.list_documents())
        print(f"Found {len(doc_refs)} document references")
        for doc_ref in doc_refs:
            print(f"  - {doc_ref.id}")
            # Try to get the document
            try:
                doc = doc_ref.get()
                if doc.exists:
                    print(f"    ✓ Document exists, fields: {list(doc.to_dict().keys()) if doc.to_dict() else 'none'}")
                else:
                    print(f"    ✗ Document does not exist (reference only)")
            except Exception as e:
                print(f"    ! Error getting document: {e}")
    except Exception as e:
        print(f"Error with list_documents(): {e}")
    
    print()
    
    if not docs and not doc_refs:
        print("\nNo documents found with either method.")
        print("\nPossible issues:")
        print("1. Service account may lack permissions")
        print("2. Documents might be in a different database")
        print("3. Collection name might be case-sensitive")
        return
    
    # Show detailed info for documents found
    print("\n=== Detailed Document Information ===\n")
    
    all_docs = docs if docs else [ref.get() for ref in doc_refs if ref.get().exists]
    
    for i, doc in enumerate(all_docs, 1):
        print(f"{i}. Document ID: {doc.id}")
        print(f"   Path: {doc.reference.path}")
        
        # Show document data
        data = doc.to_dict()
        if data:
            print(f"   Fields ({len(data)}): {list(data.keys())}")
            # Show a sample of the data
            for key, value in list(data.items())[:3]:
                value_str = str(value)[:100]
                print(f"   - {key}: {value_str}...")
        else:
            print("   (No fields - might be a container document)")
        
        # Check for subcollections
        subcollections = list(doc.reference.collections())
        if subcollections:
            print(f"   Subcollections: {[sub.id for sub in subcollections]}")
            for sub in subcollections:
                sub_docs = list(sub.limit(5).stream())
                print(f"     - {sub.id}: {len(sub_docs)} documents (showing first 5)")
                for sub_doc in sub_docs[:3]:
                    print(f"       * {sub_doc.id}")
        
        print()


if __name__ == "__main__":
    list_documents()
