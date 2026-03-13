"""
Central configuration for EagleAgent.

This file contains non-secret configuration values that are constant across environments.
These values are version-controlled and visible, making them easy to audit and maintain.

Secret values (API keys, OAuth secrets, etc.) should remain in environment variables
and GitHub Secrets, NOT in this file.

Configuration can be overridden by environment variables if needed.
"""
import os
from dotenv import load_dotenv

# Load environment variables early so class-level os.getenv calls work
load_dotenv()

class Config:
    """Application configuration settings"""
    
    # ==================== Data Storage Settings ====================
    
    # Root directory for persistent data (attachments, uploads etc)
    DATA_DIR = os.getenv("DATA_DIR", "./data")
    
    # Directory for importing CSVs
    IMPORT_DIR = os.getenv("IMPORT_DIR", "./data/import")
    
    # Database URL
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/eagleagent"  # Local dev default
    )
    
    # Checkpoint Database URL (LangGraph doesn't use the asyncpg standard style natively by default in the same way, but let's provide a raw DB URL just in case)
    CHECKPOINT_DATABASE_URL = os.getenv(
        "CHECKPOINT_DATABASE_URL",
        "postgres://postgres:postgres@localhost:5432/eagleagent"  # Local dev default for psycopg pooling
    )
    
    # Production Database URL (Optional, for running local scripts against Railway)
    PROD_DATABASE_URL = os.getenv("PROD_DATABASE_URL", "")
    
    
    # ==================== OAuth Settings ====================
    
    # Allowed Google Workspace domains (comma-separated)
    # Only users from these domains can authenticate
    OAUTH_ALLOWED_DOMAINS = os.getenv(
        "OAUTH_ALLOWED_DOMAINS", 
        "mooball.net,eagle-exports.com"
    )
    
    
    # ==================== Model Configuration ====================
    # Set the Gemini Embeddings model string.
    EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "models/gemini-embedding-2-preview")
    
    # Default LLM model to use
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-3-flash-preview")
    
    # Model temperature (0.0 - 1.0)
    DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.7"))
    
    # Max tokens for model responses
    DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "8192"))
    
    # LangGraph Execution Recursion Limit (max steps before loop aborts)
    GRAPH_RECURSION_LIMIT = int(os.getenv("GRAPH_RECURSION_LIMIT", "50"))
    
    
    # ==================== Application Settings ====================
    
    # Comma-separated list of admin email addresses
    ADMIN_EMAILS = os.getenv("ADMIN_EMAILS", "")
    
    # Temporary files upload folder
    TEMP_FILES_FOLDER = os.getenv("TEMP_FILES_FOLDER", ".files")
    
    # Chainlit URL (set after deployment, or localhost for dev)
    CHAINLIT_URL = os.getenv("CHAINLIT_URL", "http://localhost:8000")
    
    
    # ==================== File Storage Settings ====================
    
    # Max file upload size in MB
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
    
    
    # ==================== Development Settings ====================
    
    # Enable debug mode
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    
    # Log level
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    
    # ==================== Helper Methods ====================
    
    @classmethod
    def get_admin_emails(cls) -> list[str]:
        """Return admin emails as a list"""
        return [email.strip().lower() for email in cls.ADMIN_EMAILS.split(",") if email.strip()]

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
            'DATABASE_URL': cls.DATABASE_URL,
            'DATA_DIR': cls.DATA_DIR,
        }
        
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        
        return True


# Create singleton instance
config = Config()


# Validate on import (optional - uncomment to enable strict validation)
# config.validate()
