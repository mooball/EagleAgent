"""
Firestore-backed implementation of LangGraph's BaseStore for cross-thread persistent memory.

This store enables long-term memory across conversation threads, perfect for:
- User profiles and preferences
- Facts the user shares ("My name is Tom")
- Long-term context that persists across conversations

Storage structure in Firestore:
    Collection: store (configurable)
    Document ID: namespace1/namespace2/.../key
    Fields:
        - value: dict (the actual data)
        - namespace: list[str] (hierarchical path)
        - key: str (unique identifier within namespace)
        - created_at: timestamp
        - updated_at: timestamp

Example usage:
    store = FirestoreStore(project_id="mooballai")
    
    # Store user profile
    await store.aput(("users",), "tom@mooball.net", {"name": "Tom", "likes": "Python"})
    
    # Retrieve user profile in any thread
    profile = await store.aget(("users",), "tom@mooball.net")
"""

from datetime import datetime, timezone
from typing import Iterable, Optional
from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath
from langgraph.store.base import BaseStore, Op, Result, GetOp, PutOp, SearchOp, ListNamespacesOp, Item, SearchItem


class FirestoreStore(BaseStore):
    """Firestore-backed store for persistent, cross-thread memory in LangGraph."""
    
    def __init__(
        self,
        project_id: str,
        collection: str = "store",
    ):
        """
        Initialize Firestore store.
        
        Args:
            project_id: Google Cloud project ID
            collection: Firestore collection name (default: "store")
        """
        super().__init__()
        self.client = firestore.Client(project=project_id)
        self.collection_name = collection
        self.collection = self.client.collection(collection)
    
    def _make_doc_id(self, namespace: tuple[str, ...], key: str) -> str:
        """Create Firestore document ID from namespace and key."""
        # Format: namespace1/namespace2/.../key
        namespace_path = "/".join(namespace)
        return f"{namespace_path}:{key}"
    
    def _doc_to_item(self, doc, namespace: tuple[str, ...], key: str) -> Optional[Item]:
        """Convert Firestore document to Item object."""
        if not doc.exists:
            return None
        
        data = doc.to_dict()
        return Item(
            value=data.get("value", {}),
            key=key,
            namespace=namespace,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
    
    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """
        Process multiple operations synchronously.
        
        Handles GetOp, PutOp, SearchOp, and ListNamespacesOp.
        """
        results = []
        
        for op in ops:
            if isinstance(op, GetOp):
                # Get a single document
                doc_id = self._make_doc_id(op.namespace, op.key)
                doc = self.collection.document(doc_id).get()
                results.append(self._doc_to_item(doc, op.namespace, op.key))
            
            elif isinstance(op, PutOp):
                # Put or delete a document
                doc_id = self._make_doc_id(op.namespace, op.key)
                doc_ref = self.collection.document(doc_id)
                
                if op.value is None:
                    # Delete operation
                    doc_ref.delete()
                else:
                    # Put operation
                    now = datetime.now(timezone.utc)
                    
                    # Get existing doc to preserve created_at
                    existing = doc_ref.get()
                    created_at = existing.get("created_at") if existing.exists else now
                    
                    doc_ref.set({
                        "value": op.value,
                        "namespace": list(op.namespace),
                        "key": op.key,
                        "created_at": created_at,
                        "updated_at": now,
                    })
                
                results.append(None)
            
            elif isinstance(op, SearchOp):
                # Search documents by namespace prefix
                namespace_prefix = "/".join(op.namespace_prefix) if op.namespace_prefix else ""
                
                # Query documents where doc ID starts with namespace prefix
                query = self.collection
                
                if namespace_prefix:
                    # Use Firestore document ID range query
                    query = query.where(
                        FieldPath.document_id(), ">=", f"{namespace_prefix}:"
                    ).where(
                        FieldPath.document_id(), "<", f"{namespace_prefix}:\uf8ff"
                    )
                
                # Apply filter if provided
                if op.filter:
                    for field, value in op.filter.items():
                        # Simple equality filter on value fields
                        query = query.where(f"value.{field}", "==", value)
                
                # Apply pagination
                query = query.limit(op.limit).offset(op.offset)
                
                # Execute query
                docs = query.stream()
                
                search_results = []
                for doc in docs:
                    data = doc.to_dict()
                    namespace = tuple(data.get("namespace", []))
                    key = data.get("key", "")
                    
                    search_results.append(SearchItem(
                        value=data.get("value", {}),
                        key=key,
                        namespace=namespace,
                        created_at=data.get("created_at"),
                        updated_at=data.get("updated_at"),
                        score=None,  # No vector search
                    ))
                
                results.append(search_results)
            
            elif isinstance(op, ListNamespacesOp):
                # List unique namespaces (simplified implementation)
                # This is a basic implementation - could be optimized with indexing
                docs = self.collection.stream()
                
                namespaces = set()
                for doc in docs:
                    data = doc.to_dict()
                    namespace = tuple(data.get("namespace", []))
                    
                    # Apply prefix filter if specified
                    if op.prefix and not namespace[:len(op.prefix)] == op.prefix:
                        continue
                    
                    # Apply suffix filter if specified
                    if op.suffix and not namespace[-len(op.suffix):] == op.suffix:
                        continue
                    
                    # Apply max_depth filter if specified
                    if op.max_depth and len(namespace) > op.max_depth:
                        namespace = namespace[:op.max_depth]
                    
                    namespaces.add(namespace)
                
                # Convert to sorted list and apply pagination
                namespace_list = sorted(list(namespaces))
                paginated = namespace_list[op.offset:op.offset + op.limit]
                results.append(paginated)
            
            else:
                # Unknown operation type
                results.append(None)
        
        return results
    
    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """
        Process multiple operations asynchronously.
        
        Note: Firestore Python SDK doesn't have true async support,
        so this calls the sync batch() method. For production with
        high load, consider using the async Firestore client library.
        """
        # Firestore Python client is synchronous, so we just call batch()
        # In a production environment with high async load, you might want
        # to use run_in_executor or an async Firestore library
        return self.batch(ops)
