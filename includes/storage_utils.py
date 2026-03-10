"""
Local storage utilities for file uploads.
Handles uploading, downloading, and deleting files from a local directory.
"""

import os
import logging
from config.settings import config
import aiofiles

logger = logging.getLogger(__name__)

def get_base_dir() -> str:
    """Get the base directory for local storage, ensuring it exists."""
    base_dir = os.path.abspath(config.DATA_DIR)
    os.makedirs(base_dir, exist_ok=True)
    return base_dir

def generate_object_key(user_id: str, file_name: str, thread_id: str = None) -> str:
    """Generate a unique object key for a file based on user and thread."""
    import time
    timestamp = int(time.time())
    if thread_id:
        return f"{user_id}/{thread_id}/{timestamp}_{file_name}"
    return f"{user_id}/{timestamp}_{file_name}"

def generate_signed_url(bucket_name: str, object_key: str, expiration_minutes: int = 15) -> str:
    """
    Generate a URL for the file.
    Since we are local, we'll just return a local file viewer endpoint or similar.
    """
    return f"/files/{object_key}"

def _get_full_path(object_key: str) -> str:
    """Get the absolute path for an object key."""
    base_dir = get_base_dir()
    safe_key = os.path.normpath('/' + object_key).lstrip('/')
    return os.path.join(base_dir, safe_key)

async def upload_file_locally(
    file_path: str,
    object_key: str,
    content_type: str = None
) -> str:
    """
    Copy a file to the persistent local storage.
    """
    import shutil
    full_path = _get_full_path(object_key)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    shutil.copy2(file_path, full_path)
    return generate_signed_url("", object_key)

async def upload_bytes_locally(
    file_bytes: bytes,
    object_key: str,
    content_type: str = None
) -> str:
    """
    Upload bytes directly to local storage.
    """
    full_path = _get_full_path(object_key)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    async with aiofiles.open(full_path, 'wb') as f:
        await f.write(file_bytes)
    return generate_signed_url("", object_key)

async def get_file_from_local(object_key: str) -> bytes:
    """
    Read file bytes from local storage.
    """
    full_path = _get_full_path(object_key)
    if os.path.exists(full_path):
        async with aiofiles.open(full_path, 'rb') as f:
            return await f.read()
    raise FileNotFoundError(f"File not found: {full_path}")

async def delete_file_from_local(object_key: str) -> bool:
    """
    Delete a file from local storage.
    """
    full_path = _get_full_path(object_key)
    if os.path.exists(full_path):
        os.remove(full_path)
        return True
    return False
