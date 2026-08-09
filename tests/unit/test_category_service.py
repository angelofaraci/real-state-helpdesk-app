"""Unit tests for `app.services.category_service` (`create_category`,
`list_categories`, `get_category`, `update_category`, `delete_category`).
`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_property_service.py`. `delete_category`'s SAVEPOINT
branching itself is covered exhaustively by `tests/unit/test_taxonomy_service.py`;
here we only confirm it delegates to the shared helper.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.category import Category
from app.models.enums import UserRole
from app.services import category_service
from app.services.category_service import ConflictError
from app.services.taxonomy_service import TaxonomyDeleteResult


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _category(scope: OrgScope, **overrides) -> Category:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Plumbing",
        description=None,
        active=True,
    )
    defaults.update(overrides)
    return Category(**defaults)


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_create_category_adds_and_flushes() -> None:
    scope = _scope()
    session = AsyncMock()
    session.add = MagicMock()

    category = await category_service.create_category(session, scope=scope, name="Plumbing")

    assert category.name == "Plumbing"
    assert category.organization_id == scope.organization_id
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_category_raises_conflict_on_duplicate_name() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await category_service.create_category(session, scope=_scope(), name="Plumbing")


@pytest.mark.asyncio
async def test_list_categories_defaults_to_active_only() -> None:
    scope = _scope()
    categories = [_category(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = categories
    session = AsyncMock()
    session.execute.return_value = execute_result

    with patch(
        "app.services.category_service.CategoryRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        result = await category_service.list_categories(session, scope=scope)

    repo.list.assert_called_once_with(active=True)
    assert result == categories


@pytest.mark.asyncio
async def test_list_categories_include_inactive_passes_no_filter() -> None:
    scope = _scope()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.return_value = execute_result

    with patch(
        "app.services.category_service.CategoryRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        await category_service.list_categories(session, scope=scope, include_inactive=True)

    repo.list.assert_called_once_with(active=None)


@pytest.mark.asyncio
async def test_get_category_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _category(scope)
    session = _session_returning_scalar(target)

    result = await category_service.get_category(session, scope=scope, category_id=target.id)

    assert result is target


@pytest.mark.asyncio
async def test_get_category_raises_not_found_when_out_of_scope() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await category_service.get_category(session, scope=_scope(), category_id=uuid4())


@pytest.mark.asyncio
async def test_update_category_applies_fields_and_flushes() -> None:
    scope = _scope()
    target = _category(scope, name="Old Name")
    session = _session_returning_scalar(target)

    updated = await category_service.update_category(
        session, scope=scope, category_id=target.id, name="New Name"
    )

    assert updated.name == "New Name"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_category_raises_conflict_on_duplicate_name() -> None:
    scope = _scope()
    target = _category(scope)
    session = _session_returning_scalar(target)
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await category_service.update_category(
            session, scope=scope, category_id=target.id, name="Duplicate"
        )


@pytest.mark.asyncio
async def test_delete_category_delegates_to_the_shared_taxonomy_helper() -> None:
    scope = _scope()
    session = AsyncMock()
    expected = TaxonomyDeleteResult(id=uuid4(), deleted=True, active=False)

    with patch(
        "app.services.category_service.delete_or_deactivate", AsyncMock(return_value=expected)
    ) as helper:
        result = await category_service.delete_category(
            session, scope=scope, category_id=expected.id
        )

    helper.assert_awaited_once()
    assert result == expected
