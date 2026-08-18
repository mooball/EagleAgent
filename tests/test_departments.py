"""Tests for includes/netsuite/departments.py — canonical department IDs."""

import pytest

from includes.netsuite.departments import (
    DEPARTMENT_BY_ID,
    DEPARTMENT_BY_LABEL,
    Department,
    department_prompt_table,
)


EXPECTED = [
    ("1", "Machine Parts"),
    ("4", "Engine Parts"),
    ("5", "Truck Parts"),
    ("7", "Tyres"),
    ("8", "Other Parts"),
    ("9", "4WD Parts"),
    ("10", "Industrial"),
    ("11", "Forklift Parts"),
    ("13", "Cosmetic"),
]


class TestDepartmentEnum:
    def test_exact_net_suite_ids(self):
        """Pin the NetSuite internal IDs — they must never drift."""
        assert [(d.value, d.label) for d in Department] == EXPECTED

    def test_nine_departments(self):
        assert len(list(Department)) == 9

    def test_labels_are_unique(self):
        labels = [d.label for d in Department]
        assert len(set(labels)) == len(labels)

    def test_every_department_has_a_description(self):
        for dept in Department:
            assert dept.description and len(dept.description) > 20

    def test_netsuite_id_property(self):
        assert Department.ENGINE_PARTS.netsuite_id == "4"

    def test_lookup_by_id(self):
        assert DEPARTMENT_BY_ID["8"] is Department.OTHER_PARTS

    def test_lookup_by_label_is_case_insensitive(self):
        assert DEPARTMENT_BY_LABEL["engine parts"] is Department.ENGINE_PARTS

    @pytest.mark.parametrize("netsuite_id,label", EXPECTED)
    def test_prompt_table_lists_every_department(self, netsuite_id, label):
        table = department_prompt_table()
        assert f"| {netsuite_id} | {label} |" in table

    def test_prompt_table_starts_with_a_header(self):
        assert department_prompt_table().startswith("| ID | Department |")
