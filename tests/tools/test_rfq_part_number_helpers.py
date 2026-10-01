"""Tests for the requested-vs-supplied part number helpers.

``effective_part_number`` and ``is_alternative_part_number`` are the single
source of truth for the RFQ supplied part number rule (task #33073 /
`.github/prompts/plan-suppliedPartNumber.prompt.md`). They are pure, so these
tests need no database.
"""

from includes.tools.rfq_crud import (
    _is_empty_part_number,
    effective_part_number,
    is_alternative_part_number,
)


class TestEffectivePartNumber:
    def test_supplied_wins_when_set(self):
        assert effective_part_number("BOLT-123", "SP-999") == "SP-999"

    def test_falls_back_to_requested(self):
        assert effective_part_number("BOLT-123", None) == "BOLT-123"
        assert effective_part_number("BOLT-123", "") == "BOLT-123"

    def test_placeholder_supplied_falls_back(self):
        for placeholder in ("tbd", "N/A", "-", "  ", "none"):
            assert effective_part_number("BOLT-123", placeholder) == "BOLT-123"

    def test_description_only_line_uses_supplied(self):
        assert effective_part_number(None, "SP-999") == "SP-999"
        assert effective_part_number("", "SP-999") == "SP-999"

    def test_none_when_both_empty(self):
        assert effective_part_number(None, None) is None
        assert effective_part_number("", "") is None
        assert effective_part_number("tbd", "n/a") is None

    def test_values_are_stripped(self):
        assert effective_part_number("  BOLT-123  ", None) == "BOLT-123"
        assert effective_part_number(None, "  SP-999 ") == "SP-999"


class TestIsAlternativePartNumber:
    def test_different_numbers_are_alternative(self):
        assert is_alternative_part_number("BOLT-123", "SP-999") is True

    def test_equal_numbers_are_not_alternative(self):
        assert is_alternative_part_number("BOLT-123", "BOLT-123") is False

    def test_separator_only_difference_is_not_alternative(self):
        assert is_alternative_part_number("BOLT-123", "BOLT123") is False

    def test_case_only_difference_is_not_alternative(self):
        assert is_alternative_part_number("bolt-123", "BOLT-123") is False

    def test_description_only_line_with_supplied_is_alternative(self):
        assert is_alternative_part_number(None, "SP-999") is True
        assert is_alternative_part_number("", "SP-999") is True

    def test_empty_or_placeholder_supplied_is_not_alternative(self):
        assert is_alternative_part_number("BOLT-123", None) is False
        assert is_alternative_part_number("BOLT-123", "") is False
        assert is_alternative_part_number("BOLT-123", "tbd") is False

    def test_empty_requested_and_empty_supplied_is_not_alternative(self):
        assert is_alternative_part_number(None, None) is False
        assert is_alternative_part_number("", "") is False


class TestIsEmptyPartNumberUnchanged:
    def test_placeholders(self):
        assert _is_empty_part_number(None) is True
        assert _is_empty_part_number("") is True
        assert _is_empty_part_number("TBD") is True
        assert _is_empty_part_number("BOLT-123") is False
