"""
Central configuration for EagleAgent.

This file contains non-secret configuration values that are constant across environments.
These values are version-controlled and visible, making them easy to audit and maintain.

Secret values (API keys, OAuth secrets, etc.) should remain in environment variables
and GitHub Secrets, NOT in this file.

Configuration can be overridden by environment variables if needed.
"""
import os


class Config:
    """Application configuration settings"""
    
    # ==================== Google Cloud Settings ====================
    
    # Google Cloud Project ID
    GCP_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "mooballai")
    
    # Google Cloud Storage Bucket for file attachments
    GCS_BUCKET_NAME = os.getenv("GCP_BUCKET_NAME", "eagleagent")
    
    
    # ==================== OAuth Settings ====================
    
    # Allowed Google Workspace domains (comma-separated)
    # Only users from these domains can authenticate
    OAUTH_ALLOWED_DOMAINS = os.getenv(
        "OAUTH_ALLOWED_DOMAINS", 
        "mooball.net,eagle-exports.com"
    )
    
    
    # ==================== Model Configuration ====================
    
    # Default LLM model to use
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-3-flash-preview")
    
    # Model temperature (0.0 - 1.0)
    DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.7"))
    
    # Max tokens for model responses
    DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "8192"))
    
    
    # ==================== Application Settings ====================
    
    # Temporary files upload folder
    TEMP_FILES_FOLDER = os.getenv("TEMP_FILES_FOLDER", ".files")
    
    # Database URL (environment-specific, uses sensible defaults)
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./chainlit_datalayer.db"  # Local dev default
    )
    
    # Chainlit URL (set after deployment, or localhost for dev)
    CHAINLIT_URL = os.getenv("CHAINLIT_URL", "http://localhost:8000")
    
    
    # ==================== File Storage Settings ====================
    
    # File retention period in days (for GCS lifecycle)
    FILE_RETENTION_DAYS = int(os.getenv("FILE_RETENTION_DAYS", "30"))
    
    # Max file upload size in MB
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
    
    
    # ==================== Firestore Settings ====================
    
    # Firestore collection names
    THREADS_COLLECTION = os.getenv("THREADS_COLLECTION", "threads")
    USER_PROFILES_COLLECTION = os.getenv("USER_PROFILES_COLLECTION", "user_profiles")
    
    # Thread TTL in days (auto-cleanup old threads)
    THREAD_TTL_DAYS = int(os.getenv("THREAD_TTL_DAYS", "90"))
    
    
    # ==================== Development Settings ====================
    
    # Enable debug mode
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    
    # Log level
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    
    # ==================== Helper Methods ====================
    
    @classmethod
    def to_dict(cls) -> dict:
        """Return all configuration values as a dictionary"""
        return {
            key: value for key, value in vars(cls).items()
            if not key.startswith('_') and key.isupper()
        }
    
    @classmethod
    def print_config(cls, mask_secrets: bool = True):
        """
        Print current configuration (useful for debugging)
        
        Args:
            mask_secrets: Whether to mask sensitive values (default: True)
        """
        print("=" * 60)
        print("EagleAgent Configuration")
        print("=" * 60)
        for key, value in sorted(cls.to_dict().items()):
            # Mask values that might be sensitive
            if mask_secrets and any(secret in key.lower() for secret in ['secret', 'key', 'password', 'token']):
                display_value = "***MASKED***"
            else:
                display_value = value
            print(f"{key:30} = {display_value}")
        print("=" * 60)
    
    @classmethod
    def validate(cls):
        """
        Validate that required configuration is present
        Raises ValueError if required config is missing
        """
        required = {
            'GCP_PROJECT_ID': cls.GCP_PROJECT_ID,
            'GCS_BUCKET_NAME': cls.GCS_BUCKET_NAME,
        }
        
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        
        return True


# Create singleton instance
config = Config()


# Validate on import (optional - uncomment to enable strict validation)
# config.validate()
