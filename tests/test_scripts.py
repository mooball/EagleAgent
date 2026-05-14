"""Tests for config/scripts.py — script registry and argument validation."""

import pytest
from config.scripts import get_script, list_scripts, validate_args


class TestGetScript:
    def test_known_script(self):
        script = get_script("sync_netsuite_suppliers")
        assert script is not None
        assert "command" in script
        assert "description" in script

    def test_unknown_script(self):
        assert get_script("nonexistent_script") is None


class TestListScripts:
    def test_returns_dict(self):
        registry = list_scripts()
        assert isinstance(registry, dict)
        assert len(registry) > 0

    def test_all_entries_have_required_keys(self):
        for name, entry in list_scripts().items():
            assert "command" in entry, f"{name} missing 'command'"
            assert "description" in entry, f"{name} missing 'description'"
            assert isinstance(entry["command"], list), f"{name} command should be a list"


class TestValidateArgs:
    def test_unknown_script_raises(self):
        with pytest.raises(ValueError, match="Unknown script"):
            validate_args("nonexistent", ["--foo"])

    def test_allowed_arg_passes(self):
        result = validate_args("sync_netsuite_suppliers", ["--since", "2026-01-01"])
        assert result == ["--since", "2026-01-01"]

    def test_disallowed_flag_raises(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_args("update_product_embeddings", ["--force"])

    def test_empty_args_passes(self):
        result = validate_args("update_product_embeddings", [])
        assert result == []

    def test_positional_values_pass(self):
        # Non-flag tokens (no leading --) should pass through
        result = validate_args("sync_netsuite_suppliers", ["--since", "7d"])
        assert "7d" in result
