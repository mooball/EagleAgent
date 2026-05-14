"""Tests for includes/supplier_categorization.py — prompt building and response parsing."""

import json

import pytest

from includes.supplier_categorization import (
    build_prompt,
    parse_response,
    load_taxonomy,
)


class TestBuildPrompt:
    def test_includes_supplier_name(self):
        prompt = build_prompt("## Taxonomy\nTest", {"name": "Acme Corp"})
        assert "Acme Corp" in prompt

    def test_includes_url(self):
        prompt = build_prompt("taxonomy", {"name": "X", "url": "https://x.com"})
        assert "https://x.com" in prompt

    def test_missing_url_shows_placeholder(self):
        prompt = build_prompt("taxonomy", {"name": "X"})
        assert "No URL available" in prompt

    def test_includes_location(self):
        prompt = build_prompt("taxonomy", {"name": "X", "city": "Sydney", "country": "AU"})
        assert "Sydney" in prompt
        assert "AU" in prompt

    def test_includes_purchase_count(self):
        prompt = build_prompt("taxonomy", {"name": "X", "purchase_count": 42})
        assert "42" in prompt

    def test_includes_taxonomy_text(self):
        tax = "## Categories\n- OEM\n- Distributor"
        prompt = build_prompt(tax, {"name": "X"})
        assert "OEM" in prompt
        assert "Distributor" in prompt

    def test_json_output_format_requested(self):
        prompt = build_prompt("tax", {"name": "X"})
        assert "JSON" in prompt


class TestParseResponse:
    def test_clean_json(self):
        raw = '{"category": "OEM", "tier": "A", "confidence": 4, "reasoning": "test"}'
        result = parse_response(raw)
        assert result["category"] == "OEM"
        assert result["confidence"] == 4

    def test_json_in_markdown_fences(self):
        raw = '```json\n{"category": "Distributor", "tier": "B", "confidence": 3, "reasoning": "x"}\n```'
        result = parse_response(raw)
        assert result["category"] == "Distributor"

    def test_triple_backtick_no_lang(self):
        raw = '```\n{"category": "OEM", "tier": "A", "confidence": 5, "reasoning": "y"}\n```'
        result = parse_response(raw)
        assert result["category"] == "OEM"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_response("not json at all")

    def test_whitespace_handling(self):
        raw = '  \n  {"category": "OEM", "tier": "A", "confidence": 1, "reasoning": "z"}  \n  '
        result = parse_response(raw)
        assert result["category"] == "OEM"


class TestLoadTaxonomy:
    def test_returns_non_empty_string(self):
        text = load_taxonomy()
        assert isinstance(text, str)
        assert len(text) > 100  # taxonomy file should be substantial
