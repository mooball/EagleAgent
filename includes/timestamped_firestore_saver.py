"""Custom FirestoreSaver with timestamp and TTL support for checkpoint management."""

from datetime import datetime, timedelta, timezone
from langgraph_checkpoint_firestore import FirestoreSaver
from google.cloud.firestore import SERVER_TIMESTAMP


class TimestampedFirestoreSaver(FirestoreSaver):
    """Extends FirestoreSaver to add server timestamps and expiration for Firestore TTL policies."""
    
    def __init__(self, *args, ttl_days: int = 7, **kwargs):
        """
        Initialize TimestampedFirestoreSaver.
        
        Args:
            ttl_days: Number of days until checkpoints expire (default: 7)
            *args, **kwargs: Arguments passed to parent FirestoreSaver
        """
        super().__init__(*args, **kwargs)
        self.ttl_days = ttl_days
    
    def _add_timestamps_to_checkpoint(self, result):
        """
        Add created_at and expire_at timestamps to checkpoint documents.
        
        This adds timestamps to both the partition document (session/thread container)
        and the checkpoint document (actual checkpoint data) for complete TTL cleanup.
        
        Args:
            result: The result from put/aput operation containing checkpoint metadata
        """
        if not result:
            return
        
        thread_id = result.get("configurable", {}).get("thread_id")
        checkpoint_ns = result.get("configurable", {}).get("checkpoint_ns", "")
        checkpoint_id = result.get("configurable", {}).get("checkpoint_id")
        
        if not (thread_id and checkpoint_id):
            return
        
        # Calculate expiration time
        expire_at = datetime.now(timezone.utc) + timedelta(days=self.ttl_days)
        
        # Prepare timestamp data
        timestamp_data = {
            "created_at": SERVER_TIMESTAMP,
            "expire_at": expire_at
        }
        
        # Add timestamps to partition document (session/thread container)
        partition_doc_ref = self.checkpoints_collection.document(f"{thread_id}_{checkpoint_ns}")
        partition_doc_ref.set(timestamp_data, merge=True)
        
        # Add timestamps to checkpoint document (actual checkpoint data)
        checkpoint_doc_ref = partition_doc_ref.collection("checkpoints").document(checkpoint_id)
        checkpoint_doc_ref.set(timestamp_data, merge=True)
    
    def put(self, config, checkpoint, metadata, new_versions):
        """Override put to add timestamp fields to checkpoint documents."""
        result = super().put(config, checkpoint, metadata, new_versions)
        self._add_timestamps_to_checkpoint(result)
        return result
    
    async def aput(self, config, checkpoint, metadata, new_versions):
        """Override aput to add timestamp fields to checkpoint documents (async version)."""
        result = await super().aput(config, checkpoint, metadata, new_versions)
        self._add_timestamps_to_checkpoint(result)
        return result
