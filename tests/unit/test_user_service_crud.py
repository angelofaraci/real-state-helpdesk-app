"""Unit tests for the org-admin user CRUD functions in `app.services.user_service`
(`list_users`, `get_user`, `update_user`, `disable_user`). These take an
`OrgScope` (not a raw `User`), following the pattern established for
`ScopedRepository` subclasses. `AsyncSession` is mocked, following the
pattern established by `tests/unit/test_user_service.py`.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.services import user_service


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


def _make_target(scope: OrgScope, **overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Jane Doe",
        email="jane@example.com",
        role=UserRole.AGENT,
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.mark.asyncio
async def test_list_users_returns_scalars_from_the_repository_query() -> None:
    scope = _scope()
    users = [_make_target(scope), _make_target(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = users
    session = AsyncMock()
    session.execute.return_value = execute_result

    assert await user_service.list_users(session, scope=scope) == users


@pytest.mark.asyncio
async def test_get_user_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _make_target(scope)
    session = _session_returning_scalar(target)

    assert await user_service.get_user(session, scope=scope, user_id=target.id) is target


@pytest.mark.asyncio
async def test_get_user_raises_not_found_when_out_of_scope() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await user_service.get_user(session, scope=_scope(), user_id=uuid4())


@pytest.mark.asyncio
async def test_update_user_applies_name_and_role() -> None:
    scope = _scope()
    target = _make_target(scope, name="Old Name")
    session = _session_returning_scalar(target)

    updated = await user_service.update_user(
        session, scope=scope, user_id=target.id, name="New Name", role=UserRole.OWNER
    )

    assert updated.name == "New Name"
    assert updated.role == UserRole.OWNER


@pytest.mark.asyncio
async def test_disable_user_sets_status_disabled_without_deleting() -> None:
    scope = _scope()
    target = _make_target(scope)
    session = _session_returning_scalar(target)

    result = await user_service.disable_user(session, scope=scope, user_id=target.id)

    assert result.status == UserStatus.DISABLED
    session.delete.assert_not_called()
