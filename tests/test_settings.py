"""
Tests for per-agent model configuration in settings.
"""

import os
import pytest
from unittest.mock import patch


class TestGetAgentModel:
    """Test per-agent model resolution via Config.get_agent_model()."""

    def test_falls_back_to_default_model(self):
        """When no agent-specific model is set, falls back to DEFAULT_MODEL."""
        from config.settings import Config
        with patch.object(Config, "BROWSER_AGENT_MODEL", ""):
            result = Config.get_agent_model("BrowserAgent")
            assert result == Config.DEFAULT_MODEL

    def test_returns_agent_specific_model(self):
        """When agent-specific model is set, uses it instead of DEFAULT_MODEL."""
        from config.settings import Config
        with patch.object(Config, "BROWSER_AGENT_MODEL", "gemini-2.5-flash"):
            result = Config.get_agent_model("BrowserAgent")
            assert result == "gemini-2.5-flash"

    def test_unknown_agent_falls_back_to_default(self):
        """Unknown agent names fall back to DEFAULT_MODEL."""
        from config.settings import Config
        result = Config.get_agent_model("UnknownAgent")
        assert result == Config.DEFAULT_MODEL

    def test_each_agent_has_own_override(self):
        """Each agent can have a different model override."""
        from config.settings import Config
        with patch.object(Config, "GENERAL_AGENT_MODEL", "gemini-model-a"), \
             patch.object(Config, "PROCUREMENT_AGENT_MODEL", "gemini-model-b"), \
             patch.object(Config, "SUPERVISOR_MODEL", "gemini-model-c"):
            assert Config.get_agent_model("GeneralAgent") == "gemini-model-a"
            assert Config.get_agent_model("ProcurementAgent") == "gemini-model-b"
            assert Config.get_agent_model("Supervisor") == "gemini-model-c"
