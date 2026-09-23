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
    
    # Gmail Add-on — OAuth Client ID for OIDC token audience verification
    # (optional; domain restriction via hd claim is sufficient for Phase 1)
    GOOGLE_ADDON_CLIENT_ID = os.getenv("GOOGLE_ADDON_CLIENT_ID", "")
    
    
    # ==================== Model Configuration ====================
    # Set the Gemini Embeddings model string.
    EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "gemini-embedding-2-preview")
    EMBEDDINGS_LOCATION = os.getenv("EMBEDDINGS_LOCATION", "us-central1")
    
    # Default LLM model to use
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemini-3-flash-preview")
    
    # Per-agent model overrides (fall back to DEFAULT_MODEL if not set)
    BROWSER_AGENT_MODEL = os.getenv("BROWSER_AGENT_MODEL", "")
    GENERAL_AGENT_MODEL = os.getenv("GENERAL_AGENT_MODEL", "")
    PROCUREMENT_AGENT_MODEL = os.getenv("PROCUREMENT_AGENT_MODEL", "")
    SYSADMIN_AGENT_MODEL = os.getenv("SYSADMIN_AGENT_MODEL", "")
    RESEARCH_AGENT_MODEL = os.getenv("RESEARCH_AGENT_MODEL", "")
    # Supervisor only picks between agents — use a fast model by default.
    # (Was gemini-2.0-flash, which now returns 404 — see
    # .github/prompts/plan-llmObservabilityAndFailover.prompt.md.)
    SUPERVISOR_MODEL = os.getenv("SUPERVISOR_MODEL", "gemini-3.5-flash-lite")
    # Supplier quote pipeline (classify, extract, interpret)
    QUOTE_PIPELINE_MODEL = os.getenv("QUOTE_PIPELINE_MODEL", "")
    # RFQ creation pipeline (extract items from customer request emails)
    RFQ_CREATION_PIPELINE_MODEL = os.getenv("RFQ_CREATION_PIPELINE_MODEL", "")

    # Vision-based item extraction (Smart Item Adder image parsing).
    # Defaults to empty — falls back to DEFAULT_MODEL (same as chat agent).
    VISION_EXTRACTION_MODEL = os.getenv("VISION_EXTRACTION_MODEL", "")

    # Model used when the primary fails. Must be a model that actually exists:
    # it was hardcoded to gemini-2.0-flash, which now 404s, making the whole
    # failover path a dead end.
    FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-3.5-flash-lite")
    # Ordered fallback ladder, tried after the primary in this order, skipping
    # any entry equal to the primary. Without this a single FALLBACK_MODEL can
    # silently be the same model as the primary (which is exactly what our
    # .env does for QUOTE_* today), leaving no failover at all.
    # Defaults are the fastest and cheapest Flash models measured on 2026-09-23.
    FALLBACK_CHAIN = [
        m.strip()
        for m in os.getenv(
            "FALLBACK_CHAIN", "gemini-3.5-flash-lite,gemini-3.6-flash"
        ).split(",")
        if m.strip()
    ]
    # Background sync loops (Gmail / NetSuite / maintenance) run on this model
    # and tier, so our own bulk work cannot contend with interactive chat turns
    # for the same quota.
    SYNC_MODEL = os.getenv("SYNC_MODEL", "gemini-3.5-flash-lite")

    # ==================== Vertex location & service tiers ====================
    # NOTE: Gemini 3.x models are only served from the global endpoint —
    # regional locations 404 for them (measured 2026-09-24). Do not pin to a
    # region without re-checking availability.
    GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

    # Vertex service tiers ("Standard" / "Priority" / "Flex").
    # Priority = highest criticality; overflow is served at Standard instead of
    # failing. Flex = ~50% cheaper, slower, for latency-tolerant background work.
    # Empty string means omit service_tier entirely (Standard behaviour).
    # Values must be the proto enum names, not "priority"/"flex".
    INTERACTIVE_SERVICE_TIER = os.getenv("INTERACTIVE_SERVICE_TIER", "")
    SYNC_SERVICE_TIER = os.getenv("SYNC_SERVICE_TIER", "")

    # ==================== LLM retry policy ====================
    # Google's retry guidance: for real-time workloads "fail fast — limit the
    # number of retry attempts so users are not left waiting indefinitely".
    # ChatGoogleGenerativeAI defaults max_retries to 6, which is where the
    # endless "Agent working..." spinner came from: our own loop added 2 more
    # on top, then fell back to a model that 404s.
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
    # Raw-SDK equivalent (google-genai HttpRetryOptions.attempts).
    LLM_SDK_RETRY_ATTEMPTS = int(os.getenv("LLM_SDK_RETRY_ATTEMPTS", "2"))
    # Overall wall-clock budget for one pipeline call across all candidates,
    # so a retry storm cannot stall a background job indefinitely.
    LLM_MAX_ATTEMPT_SECONDS = float(os.getenv("LLM_MAX_ATTEMPT_SECONDS", "45"))
    LLM_REQUEST_TIMEOUT_MS = int(os.getenv("LLM_REQUEST_TIMEOUT_MS", "120000"))

    # ==================== LLM telemetry ====================
    # Records every LLM call to the llm_call_log table (plus a log line).
    # Best-effort: telemetry failure never fails a request.
    LLM_TELEMETRY_ENABLED = os.getenv("LLM_TELEMETRY_ENABLED", "1").lower() not in (
        "0", "false", "no", "off",
    )
    # Days of history to keep; enforced by scripts/prune_llm_call_log.py.
    LLM_TELEMETRY_RETENTION_DAYS = int(os.getenv("LLM_TELEMETRY_RETENTION_DAYS", "30"))

    # Model temperature (0.0 - 1.0)
    DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.7"))
    
    # Max tokens for model responses
    DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "8192"))
    
    # Max estimated tokens to retain in conversation history (trimming).
    # Uses a character-based approximation (1 token ≈ 4 chars).
    MAX_HISTORY_TOKENS = int(os.getenv("MAX_HISTORY_TOKENS", "60000"))
    
    # LangGraph Execution Recursion Limit (max steps before loop aborts)
    GRAPH_RECURSION_LIMIT = int(os.getenv("GRAPH_RECURSION_LIMIT", "50"))
    
    
    # ==================== Application Settings ====================
    
    # Comma-separated list of admin email addresses
    ADMIN_EMAILS = os.getenv("ADMIN_EMAILS", "")
    
    # Temporary files upload folder
    TEMP_FILES_FOLDER = os.getenv("TEMP_FILES_FOLDER", ".files")

    # Display timezone (IANA name, e.g. "Australia/Brisbane")
    TIMEZONE = os.getenv("TIMEZONE", "Australia/Brisbane")
    
    
    # ==================== File Storage Settings ====================
    
    # Max file upload size in MB
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
    
    # Email attachment limits (Gmail caps a message at 25MB; base64 adds ~33%)
    EMAIL_ATTACHMENT_MAX_MB = int(os.getenv("EMAIL_ATTACHMENT_MAX_MB", "10"))     # per file
    EMAIL_ATTACHMENT_TOTAL_MB = int(os.getenv("EMAIL_ATTACHMENT_TOTAL_MB", "18"))  # per email, raw
    EMAIL_UPLOAD_TTL_HOURS = int(os.getenv("EMAIL_UPLOAD_TTL_HOURS", "6"))
    
    
    # ==================== NetSuite Integration ====================
    
    # NetSuite account ID
    NETSUITE_ACCOUNT_ID = os.getenv("NETSUITE_ACCOUNT_ID", "794882")
    
    # OAuth2 client ID (from integration record)
    NETSUITE_CLIENT_ID = os.getenv("NETSUITE_CLIENT_ID", "")
    
    # Certificate ID (kid claim for JWT header)
    NETSUITE_CERTIFICATE_ID = os.getenv("NETSUITE_CERTIFICATE_ID", "")
    
    # Base64-encoded PEM private key for signing JWTs
    NETSUITE_PRIVATE_KEY_B64 = os.getenv("NETSUITE_PRIVATE_KEY_B64", "")
    
    # Batch size for NetSuite sync commits (rows per commit)
    NETSUITE_SYNC_BATCH_SIZE = int(os.getenv("NETSUITE_SYNC_BATCH_SIZE", "500"))
    
    
    # ==================== HubSpot Integration ====================
    
    # Private App access token (long-lived, no refresh needed)
    HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    
    
    # ==================== Gmail Integration ====================
    
    # Enable background Gmail mailbox sync (every 5 min). Disable for dev/staging.
    GMAIL_SYNC_ENABLED = os.getenv("GMAIL_SYNC_ENABLED", "false").lower() == "true"
    
    # Sync interval in seconds (default 5 minutes)
    GMAIL_SYNC_INTERVAL = int(os.getenv("GMAIL_SYNC_INTERVAL", "300"))
    
    # Restrict outbound email to these domains (comma-separated). Empty = no restriction.
    # Use on dev/staging to prevent accidental sends to real addresses.
    GMAIL_ALLOW_DOMAINS = os.getenv("GMAIL_ALLOW_DOMAINS", "")
    
    
    # ==================== NetSuite Integration ====================
    
    # Enable background NetSuite entity sync (every 5 min). Disable for dev/staging.
    NETSUITE_SYNC_ENABLED = os.getenv("NETSUITE_SYNC_ENABLED", "false").lower() == "true"
    
    # Sync interval in seconds (default 5 minutes = 300s)
    NETSUITE_SYNC_INTERVAL = int(os.getenv("NETSUITE_SYNC_INTERVAL", "300"))
    
    
    # ==================== Maintenance / Pruning ====================
    
    # Enable background maintenance loop (checkpoint + attachment pruning).
    MAINTENANCE_ENABLED = os.getenv("MAINTENANCE_ENABLED", "true").lower() == "true"
    
    # Maintenance interval in seconds (default 24 hours)
    MAINTENANCE_INTERVAL = int(os.getenv("MAINTENANCE_INTERVAL", "86400"))
    
    # Delete LangGraph checkpoints for threads older than this many days
    CHECKPOINT_RETENTION_DAYS = int(os.getenv("CHECKPOINT_RETENTION_DAYS", "90"))
    
    # Delete orphaned file attachments (not referenced by any thread) older than this many days
    ATTACHMENT_RETENTION_DAYS = int(os.getenv("ATTACHMENT_RETENTION_DAYS", "90"))
    
    
    # ==================== Development Settings ====================
    
    # Enable debug mode
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    
    # Log level
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    
    # ==================== Supply Chain Taxonomy ====================
    
    # Ordered list of (tier, tier_label, category) tuples.
    # The categorization script and dashboard UI both derive their options from this.
    # NOTE: If you add/rename categories here, also update
    #       docs/supplier-categorization-taxonomy.md (the LLM prompt source of truth).
    SUPPLY_CHAIN_TAXONOMY = [
        ("A", "Primary Sources", "OEM"),
        ("A", "Primary Sources", "Aftermarket Manufacturer"),
        ("B", "Industrial Trade Partners", "Trade Wholesaler"),
        ("B", "Industrial Trade Partners", "Authorized Dealer"),
        ("B", "Industrial Trade Partners", "Machine Dismantler / Workshop / Parts"),
        ("C", "General Commercial", "Retail / Trade Outlet"),
        ("C", "General Commercial", "Online Distributor"),
        ("C", "General Commercial", "Sourcing Broker"),
        ("D", "Retail Outlets", "B2C Retailer"),
        ("D", "Retail Outlets", "Hardware / Big Box"),
    ]

    @classmethod
    def get_supply_chain_options(cls) -> list[dict]:
        """Return taxonomy as a list of {value, label} dicts for dropdowns."""
        return [
            {
                "value": f"{tier}|{category}",
                "label": f"Tier {tier} — {category}",
                "tier": tier,
                "category": category,
            }
            for tier, _tier_label, category in cls.SUPPLY_CHAIN_TAXONOMY
        ]

    @classmethod
    def get_valid_categories(cls) -> list[str]:
        """Return flat list of valid category names."""
        return [cat for _, _, cat in cls.SUPPLY_CHAIN_TAXONOMY]

    @classmethod
    def get_valid_tiers(cls) -> list[str]:
        """Return deduplicated ordered list of tier letters."""
        seen = set()
        tiers = []
        for t, _, _ in cls.SUPPLY_CHAIN_TAXONOMY:
            if t not in seen:
                seen.add(t)
                tiers.append(t)
        return tiers

    
    # ==================== Helper Methods ====================
    
    @classmethod
    def get_agent_model(cls, agent_name: str) -> str:
        """Get the model for a specific agent, falling back to DEFAULT_MODEL."""
        agent_model_map = {
            "BrowserAgent": cls.BROWSER_AGENT_MODEL,
            "GeneralAgent": cls.GENERAL_AGENT_MODEL,
            "ProcurementAgent": cls.PROCUREMENT_AGENT_MODEL,
            "SysAdminAgent": cls.SYSADMIN_AGENT_MODEL,
            "ResearchAgent": cls.RESEARCH_AGENT_MODEL,
            "Supervisor": cls.SUPERVISOR_MODEL,
        }
        model = agent_model_map.get(agent_name, "")
        return model if model else cls.DEFAULT_MODEL

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
