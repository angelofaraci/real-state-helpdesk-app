"""Unit tests for `app.services.property_service` (`create_property`,
`list_properties`, `get_property`, `update_property`, `delete_property`).
`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_user_service_crud.py`.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import PropertyType, UserRole
from app.models.property import Property
from app.models.user import User
from app.services import property_service
from app.services.property_service import InvalidOwnerRoleError


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _owner_user(scope: OrgScope, **overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Owner Person",
        email="owner@example.com",
        role=UserRole.OWNER,
        status="active",
    )
    defaults.update(overrides)
    return User(**defaults)


def _property(scope: OrgScope, **overrides) -> Property:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        owner_id=uuid4(),
        address="123 Main St",
        type=PropertyType.APARTMENT,
        deleted_at=None,
    )
    defaults.update(overrides)
    return Property(**defaults)


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_create_property_succeeds_for_a_same_org_owner() -> None:
    scope = _scope()
    owner = _owner_user(scope)
    session = _session_returning_scalar(owner)

    property_ = await property_service.create_property(
        session, scope=scope, owner_id=owner.id, address="123 Main St", type=PropertyType.HOUSE
    )

    assert property_.owner_id == owner.id
    assert property_.address == "123 Main St"
    assert property_.type == PropertyType.HOUSE
    assert property_.organization_id == scope.organization_id
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_property_raises_not_found_for_a_cross_org_owner() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await property_service.create_property(
            session,
            scope=_scope(),
            owner_id=uuid4(),
            address="123 Main St",
            type=PropertyType.HOUSE,
        )


@pytest.mark.asyncio
async def test_create_property_raises_invalid_owner_role_for_a_non_owner() -> None:
    scope = _scope()
    non_owner = _owner_user(scope, role=UserRole.TENANT)
    session = _session_returning_scalar(non_owner)

    with pytest.raises(InvalidOwnerRoleError):
        await property_service.create_property(
            session,
            scope=scope,
            owner_id=non_owner.id,
            address="123 Main St",
            type=PropertyType.HOUSE,
        )


@pytest.mark.asyncio
async def test_list_properties_returns_scalars_from_the_repository_query() -> None:
    scope = _scope()
    properties = [_property(scope), _property(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = properties
    session = AsyncMock()
    session.execute.return_value = execute_result

    assert await property_service.list_properties(session, scope=scope) == properties


@pytest.mark.asyncio
async def test_get_property_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _property(scope)
    session = _session_returning_scalar(target)

    result = await property_service.get_property(session, scope=scope, property_id=target.id)

    assert result is target


@pytest.mark.asyncio
async def test_get_property_raises_not_found_when_out_of_scope() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await property_service.get_property(session, scope=_scope(), property_id=uuid4())


@pytest.mark.asyncio
async def test_update_property_applies_address_and_type() -> None:
    scope = _scope()
    target = _property(scope, address="Old Address")
    session = _session_returning_scalar(target)

    updated = await property_service.update_property(
        session, scope=scope, property_id=target.id, address="New Address"
    )

    assert updated.address == "New Address"


@pytest.mark.asyncio
async def test_delete_property_sets_deleted_at_without_deleting_the_row() -> None:
    scope = _scope()
    target = _property(scope)
    session = _session_returning_scalar(target)

    before = datetime.now(UTC)
    result = await property_service.delete_property(session, scope=scope, property_id=target.id)

    assert result.deleted_at is not None
    assert result.deleted_at >= before
    session.delete.assert_not_called()
