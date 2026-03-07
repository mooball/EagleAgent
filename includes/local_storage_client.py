import logging
import os
import aiofiles
from typing import Union, Dict, Any, Optional
from chainlit.data.storage_clients.base import BaseStorageClient
from config.settings import config

logger = logging.getLogger(__name__)

class LocalStorageClient(BaseStorageClient):
    """
    Local file system storage client for Chainlit data layer.
    Saves file attachments to a local directory instead of a cloud bucket.
    """
    
    def __init__(self, base_dir: str):
        """
        Initialize the local storage client.
        
        Args:
            base_dir: Base directory to store files (e.g., config.DATA_DIR + "/attachments")
        """
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        logger.info(f"Initialized LocalStorageClient at {self.base_dir}")

    def _get_full_path(self, object_key: str) -> str:
        # Sanitize object key to prevent directory traversal
        safe_key = os.path.normpath('/' + object_key).lstrip('/')
        return os.path.join(self.base_dir, safe_key)

    async def upload_file(
        self,
        object_key: str,
        data: Union[bytes, str],
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file to local storage.
        """
        try:
            full_path = self._get_full_path(object_key)
            
            # Ensure the directory exists
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            # Write data to file
            mode = 'wb' if isinstance(data, bytes) else 'w'
            
            if not overwrite and os.path.exists(full_path):
                logger.warning(f"File {object_key} already exists and overwrite is False")
                return {"url": self._get_url(object_key), "object_key": object_key}
                
            async with aiofiles.open(full_path, mode) as f:
                await f.write(data)
                
            logger.info(f"Successfully saved {object_key} locally")
            
            return {
                "url": self._get_url(object_key),
                "object_key": object_key,
                "mime": mime
            }
        except Exception as e:
            logger.error(f"Failed to save {object_key} locally: {e}")
            raise

    async def delete_file(self, object_key: str) -> bool:
        """
        Delete a file from local storage.
        """
        try:
            full_path = self._get_full_path(object_key)
            if os.path.exists(full_path):
                os.remove(full_path)
                logger.info(f"Successfully deleted {object_key} locally")
                return True
            else:
                logger.warning(f"File {object_key} not found for deletion")
                return False
        except Exception as e:
            logger.error(f"Failed to delete {object_key} locally: {e}")
            return False

    async def get_read_url(self, object_key: str) -> str:
        """
        Get a URL to read the file.
        In local development, we could serve these files directly from Chainlit or a local file path.
        Chainlit's built-in file server expects an accessible URL.
        For simple local paths, we could use file:// or relative paths if Chainlit allows it.
        We'll use a relative path URL that Chainlit will serve if we mount it, or just generic URL.
        Actually, chainlit might use `/files/{object_key}` if we mount a custom endpoint, or we can just return a direct file path.
        For now, let's return a dummy URL or local file path URL.
        """
        return self._get_url(object_key)
        
    def _get_url(self, object_key: str) -> str:
        # If we serve this directory through Chainlit, it could be something like /download/object_key
        # For local, you might be able to get away with a direct path, but let's provide a valid relative URL
        return f"/files/{object_key}"

    async def close(self) -> None:
        """Cleanup resources if needed."""
        pass
