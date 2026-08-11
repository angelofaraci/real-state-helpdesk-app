"""Unit tests for `app.core.taxonomy_defaults` — the default taxonomy seed
data. Pure data module (no session/repository imports), so a future
classifier can import label names without pulling in the service layer.
"""

from app.core.taxonomy_defaults import (
    DEFAULT_CATEGORIES,
    DEFAULT_URGENCY_LEVELS,
    CategorySeed,
    UrgencySeed,
)


def test_default_categories_has_the_six_exact_entries() -> None:
    assert len(DEFAULT_CATEGORIES) == 6
    assert all(isinstance(entry, CategorySeed) for entry in DEFAULT_CATEGORIES)
    names = [entry.name for entry in DEFAULT_CATEGORIES]
    assert names == [
        "Maintenance",
        "Billing",
        "Contract",
        "Administrative",
        "Complaints",
        "Sales inquiry",
    ]


def test_default_categories_have_non_empty_descriptions() -> None:
    assert all(entry.description for entry in DEFAULT_CATEGORIES)


def test_default_urgency_levels_has_the_four_exact_entries() -> None:
    assert len(DEFAULT_URGENCY_LEVELS) == 4
    assert all(isinstance(entry, UrgencySeed) for entry in DEFAULT_URGENCY_LEVELS)
    assert [(e.name, e.sla_hours, e.sort_order) for e in DEFAULT_URGENCY_LEVELS] == [
        ("Critical", 2, 1),
        ("High", 24, 2),
        ("Medium", 48, 3),
        ("Low", 120, 4),
    ]
