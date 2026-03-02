"""
MCP Server Configuration Loader

Loads MCP server configurations from YAML files and prepares them for use with
MultiServerMCPClient from langchain-mcp-adapters.

Supports environment variable interpolation using ${VAR_NAME} syntax.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


def _interpolate_env_vars(value: Any) -> Any:
    """
    Recursively interpolate environment variables in configuration values.
    
    Supports ${VAR_NAME} syntax. Missing variables are left unchanged with a warning.
    
    Args:
        value: Configuration value (can be str, dict, list, or other types)
        
    Returns:
        Value with environment variables interpolated
    """
    if isinstance(value, str):
        # Find all ${VAR_NAME} patterns
        pattern = r'\$\{([^}]+)\}'
        matches = re.findall(pattern, value)
        
        for var_name in matches:
            env_value = os.getenv(var_name)
            if env_value is not None:
                value = value.replace(f'${{{var_name}}}', env_value)
            else:
                logger.warning(
                    f"Environment variable '{var_name}' not found. "
                    f"Placeholder will remain in config."
                )
        
        return value
    
    elif isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    
    elif isinstance(value, list):
        return [_interpolate_env_vars(item) for item in value]
    
    else:
        return value


def _validate_server_config(name: str, config: Dict[str, Any]) -> bool:
    """
    Validate a single MCP server configuration.
    
    Args:
        name: Server name
        config: Server configuration dict
        
    Returns:
        True if valid, False otherwise
    """
    transport = config.get("transport")
    
    if not transport:
        logger.error(f"Server '{name}': Missing required field 'transport'")
        return False
    
    valid_transports = ["stdio", "http", "sse"]
    if transport not in valid_transports:
        logger.error(
            f"Server '{name}': Invalid transport '{transport}'. "
            f"Must be one of: {valid_transports}"
        )
        return False
    
    # STDIO requires command
    if transport == "stdio":
        if "command" not in config:
            logger.error(f"Server '{name}': STDIO transport requires 'command' field")
            return False
    
    # HTTP/SSE requires url
    elif transport in ["http", "sse"]:
        if "url" not in config:
            logger.error(f"Server '{name}': {transport.upper()} transport requires 'url' field")
            return False
    
    return True


def load_mcp_config(config_path: str = "config/mcp_servers.yaml") -> Dict[str, Dict[str, Any]]:
    """
    Load MCP server configurations from YAML file.
    
    The YAML file should have the following structure:
    
    ```yaml
    servers:
      server_name:
        transport: stdio  # or http, sse
        command: npx      # for stdio
        args: ["-y", "@modelcontextprotocol/server-filesystem"]
        env:              # optional environment variables
          KEY: value
      
      another_server:
        transport: http
        url: https://api.example.com/mcp
        headers:          # optional headers
          Authorization: "Bearer ${TOKEN}"
    ```
    
    Args:
        config_path: Path to YAML config file (relative to project root)
        
    Returns:
        Dictionary of server configurations compatible with MultiServerMCPClient.
        Returns empty dict if file doesn't exist or on error.
    """
    # Convert to absolute path if relative
    if not os.path.isabs(config_path):
        # Assume relative to project root (parent of includes/)
        project_root = Path(__file__).parent.parent
        config_path = project_root / config_path
    else:
        config_path = Path(config_path)
    
    # Check if file exists
    if not config_path.exists():
        logger.info(
            f"MCP config file not found at {config_path}. "
            "MCP servers will not be initialized."
        )
        return {}
    
    try:
        # Load YAML
        with open(config_path, "r") as f:
            raw_config = yaml.safe_load(f)
        
        if not raw_config:
            logger.warning(f"MCP config file at {config_path} is empty")
            return {}
        
        # Extract servers section
        if "servers" not in raw_config:
            logger.error(
                f"MCP config file at {config_path} missing 'servers' section. "
                "Expected structure: servers:\n  server_name:\n    transport: ..."
            )
            return {}
        
        servers = raw_config["servers"]
        
        if not isinstance(servers, dict):
            logger.error("'servers' section must be a dictionary")
            return {}
        
        # Interpolate environment variables
        servers = _interpolate_env_vars(servers)
        
        # Validate each server config
        validated_servers = {}
        for name, config in servers.items():
            if _validate_server_config(name, config):
                validated_servers[name] = config
                logger.info(f"Loaded MCP server config: {name} ({config.get('transport')})")
            else:
                logger.warning(f"Skipping invalid server config: {name}")
        
        return validated_servers
    
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse MCP config YAML: {e}")
        return {}
    
    except Exception as e:
        logger.error(f"Error loading MCP config from {config_path}: {e}")
        return {}


def get_mcp_tools_count(servers_config: Dict[str, Dict[str, Any]]) -> int:
    """
    Get the expected number of MCP servers from configuration.
    
    Useful for logging/debugging.
    
    Args:
        servers_config: Server configuration dict from load_mcp_config()
        
    Returns:
        Number of configured servers
    """
    return len(servers_config)
