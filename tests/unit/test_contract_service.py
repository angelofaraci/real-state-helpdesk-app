"""Unit tests for `app.services.contract_service` (`create_contract`,
`list_contracts`, `get_contract`, `update_contract`). `AsyncSession` is
mocked; `create_contract` needs two sequential lookups (property, then
tenant), so the mocked session's `execute` is configured with
`side_effect` to return them in order.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.contract import Contract
from app.models.enums import ContractStatus, PropertyType, UserRole
from app.models.property import Property
from app.models.user import User
from app.services import contract_service
from app.services.contract_service import InvalidTenantRoleError


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


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


def _tenant_user(scope: OrgScope, **overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Tenant Person",
        email="tenant@example.com",
        role=UserRole.TENANT,
        status="active",
    )
    defaults.update(overrides)
    return User(**defaults)


def _contract(scope: OrgScope, **overrides) -> Contract:
    defaults = dict(
        id=uuid4(),
        property_id=uuid4(),
        tenant_id=uuid4(),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        status=ContractStatus.ACTIVE,
    )
    defaults.update(overrides)
    return Contract(**defaults)


def _session_with_scalar_sequence(*scalar_results) -> AsyncMock:
    session = AsyncMock()
    results = []
    for scalar_result in scalar_results:
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar_result
        results.append(execute_result)
    session.execute.side_effect = results
    return session


@pytest.mark.asyncio
async def test_create_contract_succeeds_for_an_in_scope_property_and_tenant() -> None:
    scope = _scope()
    property_ = _property(scope)
    tenant = _tenant_user(scope)
    session = _session_with_scalar_sequence(property_, tenant)

    contract = await contract_service.create_contract(
        session,
        scope=scope,
        property_id=property_.id,
        tenant_id=tenant.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )

    assert contract.property_id == property_.id
    assert contract.tenant_id == tenant.id
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_contract_raises_not_found_for_a_cross_org_property() -> None:
    session = _session_with_scalar_sequence(None)

    with pytest.raises(NotFoundError):
        await contract_service.create_contract(
            session,
            scope=_scope(),
            property_id=uuid4(),
            tenant_id=uuid4(),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )


@pytest.mark.asyncio
async def test_create_contract_raises_not_found_for_a_cross_org_tenant() -> None:
    scope = _scope()
    property_ = _property(scope)
    session = _session_with_scalar_sequence(property_, None)

    with pytest.raises(NotFoundError):
        await contract_service.create_contract(
            session,
            scope=scope,
            property_id=property_.id,
            tenant_id=uuid4(),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )


@pytest.mark.asyncio
async def test_create_contract_raises_invalid_tenant_role_for_a_non_tenant() -> None:
    scope = _scope()
    property_ = _property(scope)
    non_tenant = _tenant_user(scope, role=UserRole.OWNER)
    session = _session_with_scalar_sequence(property_, non_tenant)

    with pytest.raises(InvalidTenantRoleError):
        await contract_service.create_contract(
            session,
            scope=scope,
            property_id=property_.id,
            tenant_id=non_tenant.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )


@pytest.mark.asyncio
async def test_list_contracts_returns_scalars_from_the_repository_query() -> None:
    scope = _scope()
    contracts = [_contract(scope), _contract(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = contracts
    session = AsyncMock()
    session.execute.return_value = execute_result

    assert await contract_service.list_contracts(session, scope=scope) == contracts


@pytest.mark.asyncio
async def test_get_contract_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _contract(scope)
    session = _session_with_scalar_sequence(target)

    result = await contract_service.get_contract(session, scope=scope, contract_id=target.id)

    assert result is target


@pytest.mark.asyncio
async def test_get_contract_raises_not_found_when_out_of_scope() -> None:
    session = _session_with_scalar_sequence(None)

    with pytest.raises(NotFoundError):
        await contract_service.get_contract(session, scope=_scope(), contract_id=uuid4())


@pytest.mark.asyncio
async def test_update_contract_applies_status_only() -> None:
    scope = _scope()
    target = _contract(scope, status=ContractStatus.ACTIVE)
    session = _session_with_scalar_sequence(target)

    updated = await contract_service.update_contract(
        session, scope=scope, contract_id=target.id, status=ContractStatus.TERMINATED
    )

    assert updated.status == ContractStatus.TERMINATED
