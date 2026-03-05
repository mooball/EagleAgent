"""
Google Cloud Storage utilities for file uploads.

Handles uploading, downloading, and deleting files from GCS bucket.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional
import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


def get_gcp_credentials():
    """
    Get GCP credentials using Application Default Credentials or service account key.
    
    In Cloud Run (production): Uses Application Default Credentials (ADC)
    In local development: Uses service account key file if GOOGLE_APPLICATION_CREDENTIALS is set
    
    Returns:
        google.auth.credentials.Credentials: GCP credentials
    """
    # Check if GOOGLE_APPLICATION_CREDENTIALS env var is set (local dev)
    if creds_file := os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        if os.path.exists(creds_file):
            logger.info(f"Using service account credentials from {creds_file}")
            return service_account.Credentials.from_service_account_file(creds_file)
    
    # Fallback to checking for service-account-key.json in project root (legacy)
    service_account_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), 
        "service-account-key.json"
    )
    
    if os.path.exists(service_account_path):
        logger.info(f"Using service account credentials from {service_account_path}")
        return service_account.Credentials.from_service_account_file(service_account_path)
    
    # Use Application Default Credentials (Cloud Run, GCE, Cloud Shell)
    logger.info("Using Application Default Credentials")
    credentials, project = google.auth.default()
    return credentials


def get_storage_client() -> storage.Client:
    """
    Initialize and return a Google Cloud Storage client.
    
    Uses Application Default Credentials in Cloud Run or service account key locally.
    
    Returns:
        storage.Client: Initialized GCS client
    """
    credentials = get_gcp_credentials()
    return storage.Client(credentials=credentials)


def upload_file_to_gcs(
    file_path: str, 
    bucket_name: str, 
    object_key: str,
    content_type: Optional[str] = None
) -> str:
    """
    Upload a file to Google Cloud Storage.
    
    Args:
        file_path: Local path to the file to upload
        bucket_name: Name of the GCS bucket
        object_key: Object key/path in the bucket (e.g., "uploads/2024/file.pdf")
        content_type: Optional MIME type of the file
        
    Returns:
        str: Public URL of the uploaded file
        
    Raises:
        Exception: If upload fails
    """
    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        
        # Upload the file
        if content_type:
            blob.upload_from_filename(file_path, content_type=content_type)
        else:
            blob.upload_from_filename(file_path)
        
        # Get public URL
        public_url = blob.public_url
        
        logger.info(f"Successfully uploaded {object_key} to {bucket_name}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to upload {object_key} to GCS: {e}")
        raise


def upload_bytes_to_gcs(
    file_bytes: bytes,
    bucket_name: str,
    object_key: str,
    content_type: Optional[str] = None
) -> str:
    """
    Upload bytes directly to Google Cloud Storage.
    
    Args:
        file_bytes: File content as bytes
        bucket_name: Name of the GCS bucket
        object_key: Object key/path in the bucket
        content_type: Optional MIME type of the file
        
    Returns:
        str: Public URL of the uploaded file
        
    Raises:
        Exception: If upload fails
    """
    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        
        # Upload bytes
        if content_type:
            blob.upload_from_string(file_bytes, content_type=content_type)
        else:
            blob.upload_from_string(file_bytes)
        
        public_url = blob.public_url
        
        logger.info(f"Successfully uploaded bytes to {object_key} in {bucket_name}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to upload bytes to GCS: {e}")
        raise


def get_file_from_gcs(bucket_name: str, object_key: str) -> bytes:
    """
    Download a file from Google Cloud Storage.
    
    Args:
        bucket_name: Name of the GCS bucket
        object_key: Object key/path in the bucket
        
    Returns:
        bytes: File content as bytes
        
    Raises:
        Exception: If download fails
    """
    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        
        file_bytes = blob.download_as_bytes()
        
        logger.info(f"Successfully downloaded {object_key} from {bucket_name}")
        return file_bytes
        
    except Exception as e:
        logger.error(f"Failed to download {object_key} from GCS: {e}")
        raise


def delete_file_from_gcs(bucket_name: str, object_key: str) -> bool:
    """
    Delete a file from Google Cloud Storage.
    
    Args:
        bucket_name: Name of the GCS bucket
        object_key: Object key/path in the bucket
        
    Returns:
        bool: True if deletion successful, False otherwise
    """
    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        
        blob.delete()
        
        logger.info(f"Successfully deleted {object_key} from {bucket_name}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to delete {object_key} from GCS: {e}")
        return False


def generate_signed_url(
    bucket_name: str, 
    object_key: str, 
    expiration_hours: int = 24
) -> str:
    """
    Generate a signed URL for private access to a GCS object.
    
    Args:
        bucket_name: Name of the GCS bucket
        object_key: Object key/path in the bucket
        expiration_hours: Hours until the URL expires (default: 24)
        
    Returns:
        str: Signed URL for the object
        
    Raises:
        Exception: If URL generation fails
    """
    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        
        kwargs = {
            "version": "v4",
            "expiration": timedelta(hours=expiration_hours),
            "method": "GET"
        }
        
        # On Cloud Run / Compute Engine, default credentials lack a private key (sign_bytes).
        # We must use the IAM signBlob API by passing the token and service account email directly.
        credentials = client._credentials
        if credentials and not hasattr(credentials, "sign_bytes"):
            if not credentials.valid:
                credentials.refresh(Request())
            
            token = credentials.token
            email = getattr(credentials, "service_account_email", None)
            
            # Fallback to fetching it directly from the metadata server if attribute is missing
            if not email:
                import urllib.request
                req = urllib.request.Request(
                    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                    headers={"Metadata-Flavor": "Google"}
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as response:
                        email = response.read().decode("utf-8").strip()
                except Exception as meta_error:
                    logger.warning(f"Could not retrieve service account email from metadata: {meta_error}")

            if token and email:
                kwargs["access_token"] = token
                kwargs["service_account_email"] = email
        
        url = blob.generate_signed_url(**kwargs)
        
        logger.info(f"Generated signed URL for {object_key}")
        return url
        
    except Exception as e:
        logger.error(f"Failed to generate signed URL for {object_key}: {e}")
        raise


def generate_object_key(user_id: str, thread_id: str, filename: str) -> str:
    """
    Generate a consistent object key for storing files in GCS.
    
    Format: uploads/{user_id}/{thread_id}/{timestamp}_{filename}
    
    Args:
        user_id: User ID
        thread_id: Thread/conversation ID
        filename: Original filename
        
    Returns:
        str: Object key for GCS storage
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_filename = filename.replace(" ", "_")
    
    return f"uploads/{user_id}/{thread_id}/{timestamp}_{safe_filename}"
