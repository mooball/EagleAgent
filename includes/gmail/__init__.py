"""Gmail integration client using domain-wide delegation.

Provides a wrapper around the Gmail API for creating drafts, sending emails,
and tracking replies via the History API.

The service account impersonates individual staff users via domain-wide delegation.
"""

import logging
import json
import base64
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import Config

logger = logging.getLogger(__name__)


class RecipientBlockedError(Exception):
    """Raised when a recipient is not in the allowed domains list."""
    pass


def check_recipient_allowed(recipient_email: str) -> None:
    """Validate recipient against GMAIL_ALLOW_DOMAINS. Raises RecipientBlockedError if blocked."""
    allow_domains = Config.GMAIL_ALLOW_DOMAINS
    if not allow_domains:
        return  # no restriction
    allowed = {d.strip().lower() for d in allow_domains.split(",") if d.strip()}
    if not allowed:
        return
    domain = recipient_email.rsplit("@", 1)[-1].lower().strip()
    if domain not in allowed:
        raise RecipientBlockedError(
            f"Recipient domain '{domain}' not in GMAIL_ALLOW_DOMAINS ({', '.join(sorted(allowed))}). "
            f"Email to {recipient_email} blocked."
        )

# Cache for service account info
_service_account_info: Optional[dict] = None
_credentials_cache: dict = {}  # user_email -> credentials


def get_service_account_info() -> dict:
    """Load and cache service account credentials from JSON file."""
    global _service_account_info
    if _service_account_info is not None:
        return _service_account_info
    
    import os
    # Check GOOGLE_APPLICATION_CREDENTIALS first (set by start.sh in Docker)
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and Path(env_path).exists():
        key_path = Path(env_path)
    else:
        key_path = Path("service-account-key.json")
    
    if not key_path.exists():
        raise FileNotFoundError(
            f"Service account key not found at {key_path.absolute()}. "
            "Expected: service-account-key.json in project root or GOOGLE_APPLICATION_CREDENTIALS env var"
        )
    
    with open(key_path) as f:
        _service_account_info = json.load(f)
    
    return _service_account_info


def get_credentials(user_email: str):
    """Get credentials for impersonating a specific user via domain-wide delegation.
    
    Args:
        user_email: Email address of the user to impersonate (must be in @eagle-exports.com domain)
        
    Returns:
        google.oauth2.service_account.Credentials with subject set to user_email
    """
    # Check cache
    if user_email in _credentials_cache:
        creds = _credentials_cache[user_email]
        # Refresh if needed
        if creds.expired:
            creds.refresh(Request())
        return creds
    
    # Load service account and create delegated credentials
    service_account_info = get_service_account_info()
    
    try:
        # Create base credentials
        base_creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=[
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ],
        )
        
        # Use with_subject() for domain-wide delegation
        creds = base_creds.with_subject(user_email)
        _credentials_cache[user_email] = creds
        return creds
    except Exception as e:
        logger.error(f"Failed to create credentials for {user_email}: {e}")
        raise


def get_gmail_client(user_email: str):
    """Get Gmail API client impersonating the given user.
    
    Args:
        user_email: Email address of the user to impersonate
        
    Returns:
        googleapiclient.discovery.Resource: Gmail API client
        
    Raises:
        FileNotFoundError: If service account key not found
        Exception: If credential creation fails
    """
    creds = get_credentials(user_email)
    return build("gmail", "v1", credentials=creds)


def test_connection(user_email: str) -> dict:
    """Test Gmail API connectivity by listing recent drafts.
    
    Args:
        user_email: Email address of the user to test
        
    Returns:
        dict with:
            - status: "ok" | "error"
            - message: human-readable summary
            - details: response data (on success) or error details
    """
    try:
        service = get_gmail_client(user_email)
        
        # Try to get user profile to verify access
        profile = service.users().getProfile(userId="me").execute()
        
        # Try to list drafts (requires gmail.modify or gmail.readonly scope)
        drafts = service.users().drafts().list(userId="me", maxResults=5).execute()
        
        return {
            "status": "ok",
            "message": f"Gmail API connected for {user_email}",
            "details": {
                "email_address": profile.get("emailAddress"),
                "messages_total": profile.get("messagesTotal"),
                "threads_total": profile.get("threadsTotal"),
                "draft_count": len(drafts.get("drafts", [])),
            },
        }
    except HttpError as e:
        error_content = json.loads(e.content.decode())
        logger.error(f"Gmail API error for {user_email}: {error_content}")
        return {
            "status": "error",
            "message": f"Gmail API error: {error_content.get('error', {}).get('message', str(e))}",
            "details": error_content,
        }
    except Exception as e:
        logger.error(f"Unexpected error testing Gmail connection for {user_email}: {e}")
        return {
            "status": "error",
            "message": f"Connection test failed: {str(e)}",
            "details": str(e),
        }
