"""Canonical RFQ item Department definitions (NetSuite department IDs).

Departments classify RFQ line items and are pushed to NetSuite Opportunity
line items (``transactionLine.department``). The values are NetSuite
internal IDs — see ``scripts/sync_netsuite_departments.py`` for the live
list they were sourced from.

The descriptions are the canonical guidance the LLM uses to classify
items, so keep them stable and unambiguous. This is a code-level enum on
purpose: the list changes rarely and must be version-controlled alongside
the matching logic. No DB table needed.
"""

from enum import Enum


class Department(str, Enum):
    """NetSuite department IDs for RFQ line items.

    ``value`` is the NetSuite internal ID; ``label`` is the NetSuite name;
    ``description`` is the classification guidance for the LLM.
    """

    MACHINE_PARTS = "1"
    ENGINE_PARTS = "4"
    TRUCK_PARTS = "5"
    TYRES = "7"
    OTHER_PARTS = "8"
    FOUR_WD_PARTS = "9"
    INDUSTRIAL = "10"
    FORKLIFT_PARTS = "11"
    COSMETIC = "13"

    @property
    def netsuite_id(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_LABELS: dict[Department, str] = {
    Department.MACHINE_PARTS: "Machine Parts",
    Department.ENGINE_PARTS: "Engine Parts",
    Department.TRUCK_PARTS: "Truck Parts",
    Department.TYRES: "Tyres",
    Department.OTHER_PARTS: "Other Parts",
    Department.FOUR_WD_PARTS: "4WD Parts",
    Department.INDUSTRIAL: "Industrial",
    Department.FORKLIFT_PARTS: "Forklift Parts",
    Department.COSMETIC: "Cosmetic",
}

_DESCRIPTIONS: dict[Department, str] = {
    Department.MACHINE_PARTS: (
        "Parts for heavy mobile/earthmoving or mining machinery not covered "
        "elsewhere (excavators, loaders, drills) — undercarriage, buckets, "
        "hydraulic rams, tracks."
    ),
    Department.ENGINE_PARTS: (
        "Components specific to an engine's internal or fuel/air/cooling "
        "systems (pistons, gaskets, filters, injectors, belts, turbos), "
        "regardless of what machine the engine sits in. Use only when the "
        "item is specifically part of the engine assembly — parts outside "
        "the engine assembly are most often Machine Parts."
    ),
    Department.TRUCK_PARTS: (
        "Parts specific to a truck's body, chassis, cab, or drivetrain (not "
        "the engine itself) — brakes, suspension, mirrors, panels, trailer "
        "hardware."
    ),
    Department.TYRES: (
        "Tyres and wheel-related items (tubes, rims, valves) for any vehicle "
        "or machine type."
    ),
    Department.OTHER_PARTS: (
        "Genuine catch-all for anything that doesn't clearly fit the above. "
        "Used sparingly rather than as a default when unsure."
    ),
    Department.FOUR_WD_PARTS: (
        "Parts specific to four-wheel-drive vehicles — drivetrain components, "
        "transfer cases, differentials, suspension, and 4WD-specific "
        "accessories."
    ),
    Department.INDUSTRIAL: (
        "Fixed-plant or general industrial equipment and supplies not tied to "
        "a specific vehicle or machine — pumps, bearings, fittings, "
        "fasteners, conveyor components."
    ),
    Department.FORKLIFT_PARTS: (
        "Parts specific to forklifts and other warehouse lift equipment — "
        "masts, forks, hydraulic lift components, counterweights."
    ),
    Department.COSMETIC: (
        "Purely aesthetic/non-functional items — decals, paint, trim, "
        "upholstery, badges."
    ),
}


DEPARTMENT_BY_ID: dict[str, Department] = {d.value: d for d in Department}
DEPARTMENT_BY_LABEL: dict[str, Department] = {d.label.lower(): d for d in Department}


def department_prompt_table() -> str:
    """Markdown table of departments for LLM classification prompts."""
    rows = [
        "| ID | Department | Description |",
        "|---|------------|-------------|",
    ]
    for dept in Department:
        rows.append(f"| {dept.value} | {dept.label} | {dept.description} |")
    return "\n".join(rows)
