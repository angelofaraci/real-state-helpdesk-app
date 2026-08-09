"""Unit tests for `CategoryRepository`, the direct-scoped repository for
`Category` (owns its own `organization_id` column).

`get_or_404`/`add`/`update`/`delete` are already covered generically by
`tests/unit/test_scoped_repository.py`; these tests cover only what
`CategoryRepository` adds: the `active` filter on `list()`, mirroring
`UserRepository.list()`'s pattern from Work Unit 4.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.category_repository import CategoryRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_list_with_no_filter_returns_every_scoped_row() -> None:
    repo = CategoryRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list())

    assert "categories" in sql
    assert "active" not in sql.split("WHERE")[-1] or "active =" not in sql


def test_list_active_true_filters_to_active_rows() -> None:
    repo = CategoryRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list(active=True))

    assert "active = true" in sql


def test_list_active_false_filters_to_inactive_rows() -> None:
    repo = CategoryRepository(AsyncMock(), _scope())

    sql = _compiled(repo.list(active=False))

    assert "active = false" in sql


@pytest.mark.asyncio
async def test_get_or_404_scopes_to_the_organization() -> None:
    scope = _scope()
    repo = CategoryRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert scope.organization_id.hex in sql
