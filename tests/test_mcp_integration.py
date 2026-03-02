"""
Integration test for MCP server support in EagleAgent.

Tests the MCP configuration loader and client initialization.
"""

import pytest
import os
import tempfile
from pathlib import Path
from includes.mcp_config import load_mcp_config, _interpolate_env_vars, _validate_server_config


class TestMCPConfigLoader:
    """Test MCP configuration loading and validation."""
    
    def test_empty_config_returns_empty_dict(self):
        """Test that empty servers config returns empty dict."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("servers: {}")
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert config == {}
        finally:
            os.unlink(temp_path)
    
    def test_missing_file_returns_empty_dict(self):
        """Test that missing config file returns empty dict without error."""
        config = load_mcp_config("/nonexistent/path/config.yaml")
        assert config == {}
    
    def test_valid_stdio_server_config(self):
        """Test loading a valid STDIO server configuration."""
        yaml_content = """
servers:
  filesystem:
    transport: stdio
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/tmp"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert "filesystem" in config
            assert config["filesystem"]["transport"] == "stdio"
            assert config["filesystem"]["command"] == "npx"
            assert len(config["filesystem"]["args"]) == 3
        finally:
            os.unlink(temp_path)
    
    def test_valid_http_server_config(self):
        """Test loading a valid HTTP server configuration."""
        yaml_content = """
servers:
  api_server:
    transport: http
    url: https://api.example.com/mcp
    headers:
      Authorization: "Bearer token123"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert "api_server" in config
            assert config["api_server"]["transport"] == "http"
            assert config["api_server"]["url"] == "https://api.example.com/mcp"
            assert "Authorization" in config["api_server"]["headers"]
        finally:
            os.unlink(temp_path)
    
    def test_env_var_interpolation(self):
        """Test that environment variables are interpolated correctly."""
        os.environ["TEST_MCP_PATH"] = "/test/path"
        os.environ["TEST_MCP_TOKEN"] = "secret123"
        
        yaml_content = """
servers:
  test_server:
    transport: stdio
    command: npx
    args:
      - "${TEST_MCP_PATH}"
    env:
      TOKEN: "${TEST_MCP_TOKEN}"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert config["test_server"]["args"][0] == "/test/path"
            assert config["test_server"]["env"]["TOKEN"] == "secret123"
        finally:
            os.unlink(temp_path)
            del os.environ["TEST_MCP_PATH"]
            del os.environ["TEST_MCP_TOKEN"]
    
    def test_invalid_transport_rejected(self):
        """Test that invalid transport types are rejected."""
        yaml_content = """
servers:
  bad_server:
    transport: invalid_transport
    command: test
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert "bad_server" not in config  # Should be skipped
        finally:
            os.unlink(temp_path)
    
    def test_stdio_without_command_rejected(self):
        """Test that STDIO server without command is rejected."""
        yaml_content = """
servers:
  bad_stdio:
    transport: stdio
    args: ["-y", "server"]
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert "bad_stdio" not in config
        finally:
            os.unlink(temp_path)
    
    def test_http_without_url_rejected(self):
        """Test that HTTP server without URL is rejected."""
        yaml_content = """
servers:
  bad_http:
    transport: http
    headers:
      Authorization: "Bearer token"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert "bad_http" not in config
        finally:
            os.unlink(temp_path)
    
    def test_multiple_servers(self):
        """Test loading multiple MCP server configurations."""
        yaml_content = """
servers:
  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  
  api:
    transport: http
    url: https://api.example.com/mcp
  
  sse_server:
    transport: sse
    url: https://sse.example.com/stream
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            config = load_mcp_config(temp_path)
            assert len(config) == 3
            assert "filesystem" in config
            assert "api" in config
            assert "sse_server" in config
        finally:
            os.unlink(temp_path)


class TestEnvVarInterpolation:
    """Test environment variable interpolation."""
    
    def test_simple_interpolation(self):
        """Test simple ${VAR} interpolation."""
        os.environ["TEST_VAR"] = "value123"
        try:
            result = _interpolate_env_vars("${TEST_VAR}")
            assert result == "value123"
        finally:
            del os.environ["TEST_VAR"]
    
    def test_multiple_vars(self):
        """Test multiple variables in one string."""
        os.environ["VAR1"] = "hello"
        os.environ["VAR2"] = "world"
        try:
            result = _interpolate_env_vars("${VAR1} ${VAR2}")
            assert result == "hello world"
        finally:
            del os.environ["VAR1"]
            del os.environ["VAR2"]
    
    def test_dict_interpolation(self):
        """Test interpolation in nested dicts."""
        os.environ["TOKEN"] = "secret"
        try:
            result = _interpolate_env_vars({
                "url": "https://api.example.com",
                "headers": {
                    "Authorization": "Bearer ${TOKEN}"
                }
            })
            assert result["headers"]["Authorization"] == "Bearer secret"
        finally:
            del os.environ["TOKEN"]
    
    def test_list_interpolation(self):
        """Test interpolation in lists."""
        os.environ["PATH"] = "/test/path"
        try:
            result = _interpolate_env_vars(["arg1", "${PATH}", "arg3"])
            assert result[1] == "/test/path"
        finally:
            del os.environ["PATH"]


class TestServerConfigValidation:
    """Test server configuration validation."""
    
    def test_valid_stdio_config(self):
        """Test validation of valid STDIO config."""
        config = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "server"]
        }
        assert _validate_server_config("test", config) is True
    
    def test_valid_http_config(self):
        """Test validation of valid HTTP config."""
        config = {
            "transport": "http",
            "url": "https://api.example.com"
        }
        assert _validate_server_config("test", config) is True
    
    def test_missing_transport(self):
        """Test rejection of config without transport."""
        config = {"command": "npx"}
        assert _validate_server_config("test", config) is False
    
    def test_invalid_transport(self):
        """Test rejection of invalid transport type."""
        config = {
            "transport": "websocket",
            "command": "test"
        }
        assert _validate_server_config("test", config) is False
