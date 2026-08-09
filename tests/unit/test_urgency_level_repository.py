"""Unit tests for `UrgencyLevelRepository`, the direct-scoped repository for
`UrgencyLevel` (owns its own `organization_id` column).

Mirrors `tests/unit/test_category_repository.py`: covers only what this
repository adds on top of the generic `ScopedRepository` behavior — the
`active` filter and `sort_order` ordering on `list()`.
"""

import uuid
from unittest.mock import AsyncMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.urgency_level_repository import UrgencyLevelRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_list_orders_by_sort_order() -> None:
    repo = UrgencyLevelRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list())

    assert "ORDER BY urgency_levels.sort_order" in sql


def test_list_active_true_filters_to_active_rows() -> None:
    repo = UrgencyLevelRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list(active=True))

    assert "active = true" in sql


def test_list_active_false_filters_to_inactive_rows() -> None:
    repo = UrgencyLevelRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list(active=False))

    assert "active = false" in sql
