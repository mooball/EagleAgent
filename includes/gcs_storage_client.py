"""
GCS Storage Client for Chainlit Data Layer.

Implements Chainlit's BaseStorageClient interface for Google Cloud Storage.
"""

import logging
from typing import Dict, Any, Union
from chainlit.data.storage_clients.base import BaseStorageClient
from includes.storage_utils import (
    upload_bytes_to_gcs,
    delete_file_from_gcs,
    generate_signed_url,
    get_storage_client
)

logger = logging.getLogger(__name__)


class GCSStorageClient(BaseStorageClient):
    """Google Cloud Storage client for Chainlit file persistence."""
    
    def __init__(self, bucket_name: str):
        """
        Initialize GCS storage client.
        
        Args:
            bucket_name: Name of the GCS bucket
        """
        self.bucket_name = bucket_name
        self.client = get_storage_client()
        logger.info(f"Initialized GCS storage client for bucket: {bucket_name}")
    
    async def upload_file(
        self,
        object_key: str,
        data: Union[bytes, str],
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> Dict[str, Any]:
        """
        Upload a file to GCS.
        
        Args:
            object_key: Path in the bucket
            data: File content (bytes or string)
            mime: MIME type
            overwrite: Whether to overwrite existing files
            content_disposition: Optional content disposition header
            
        Returns:
            dict with object_key and url
        """
        try:
            # Convert string to bytes if needed
            if isinstance(data, str):
                data = data.encode('utf-8')
            
            # Upload to GCS
            bucket = self.client.bucket(self.bucket_name)
            blob = bucket.blob(object_key)
            
            # Check if file exists and overwrite is False
            if not overwrite and blob.exists():
                logger.warning(f"File {object_key} already exists and overwrite=False")
                # Return existing file info
                signed_url = generate_signed_url(self.bucket_name, object_key, expiration_hours=168)
                return {
                    "object_key": object_key,
                    "url": signed_url
                }
            
            # Upload the file
            blob.upload_from_string(data, content_type=mime)
            
            # Set content disposition if provided
            if content_disposition:
                blob.content_disposition = content_disposition
                blob.patch()
            
            # Generate signed URL (7 days expiration)
            signed_url = generate_signed_url(self.bucket_name, object_key, expiration_hours=168)
            
            logger.info(f"Uploaded file to GCS: {object_key}")
            
            return {
                "object_key": object_key,
                "url": signed_url
            }
            
        except Exception as e:
            logger.error(f"Failed to upload file to GCS: {e}")
            raise
    
    async def delete_file(self, object_key: str) -> bool:
        """
        Delete a file from GCS.
        
        Args:
            object_key: Path in the bucket
            
        Returns:
            True if successful, False otherwise
        """
        try:
            success = delete_file_from_gcs(self.bucket_name, object_key)
            if success:
                logger.info(f"Deleted file from GCS: {object_key}")
            return success
        except Exception as e:
            logger.error(f"Failed to delete file from GCS: {e}")
            return False
    
    async def get_read_url(self, object_key: str) -> str:
        """
        Generate a signed URL for reading a file.
        
        Args:
            object_key: Path in the bucket
            
        Returns:
            Signed URL (valid for 7 days)
        """
        try:
            signed_url = generate_signed_url(self.bucket_name, object_key, expiration_hours=168)
            logger.debug(f"Generated signed URL for {object_key}")
            return signed_url
        except Exception as e:
            logger.error(f"Failed to generate signed URL: {e}")
            raise
    
    async def close(self) -> None:
        """Clean up resources (GCS client doesn't need explicit cleanup)."""
        logger.debug("GCS storage client closed")
        pass
