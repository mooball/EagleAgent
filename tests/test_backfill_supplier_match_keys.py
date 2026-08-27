"""Tests for scripts/backfill_supplier_match_keys.py — alt_names marker cleanup (pure)."""

from scripts.backfill_supplier_match_keys import clean_alt_names


def test_strips_marker_keeping_other_names():
    cleaned, changed = clean_alt_names(["__dedup_reviewed__", "Acme Fluid"])
    assert changed is True
    assert cleaned == ["Acme Fluid"]


def test_none_when_only_marker():
    cleaned, changed = clean_alt_names(["__dedup_reviewed__"])
    assert changed is True
    assert cleaned is None


def test_untouched_when_no_markers():
    cleaned, changed = clean_alt_names(["Acme Fluid", "ACME"])
    assert changed is False
    assert cleaned == ["Acme Fluid", "ACME"]


def test_none_input_untouched():
    cleaned, changed = clean_alt_names(None)
    assert changed is False
    assert cleaned is None


def test_non_list_input_untouched():
    cleaned, changed = clean_alt_names("__dedup_reviewed__")
    assert changed is False
    assert cleaned == "__dedup_reviewed__"
