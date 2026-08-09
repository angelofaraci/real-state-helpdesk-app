"""Contract service: org-scoped contract CRUD (join-scoped via property).

`create_contract` validates `property_id` through
`PropertyRepository.get_or_404` (a cross-org or soft-deleted property
surfaces as `app.core.exceptions.NotFoundError`, 404) and `tenant_id`
through `UserRepository.get_or_404` (a cross-org tenant likewise surfaces
as 404 — the same existence-hiding rule applied everywhere else). A
same-org user referenced by `tenant_id` whose role is not `TENANT` is a
business-rule validation failure, not a scope violation, so it raises
`InvalidTenantRoleError` (422) instead.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.contract import Contract
from app.models.enums import UserRole
from app.repositories.contract_repository import ContractRepository
from app.repositories.property_repository import PropertyRepository
from app.repositories.user_repository import UserRepository


class ContractServiceError(Exception):
    """Base class for contract service failures."""


class InvalidTenantRoleError(ContractServiceError):
    """Raised when `tenant_id` refers to a same-org user whose role is not
    `TENANT`."""


async def create_contract(
    session: AsyncSession,
    *,
    scope: OrgScope,
    property_id: UUID,
    tenant_id: UUID,
    start_date: date,
    end_date: date,
) -> Contract:
    """Create a contract linking `tenant_id` to `property_id`, both scoped
    to `scope`'s organization.

    Raises `app.core.exceptions.NotFoundError` if `property_id` does not
    exist, is soft-deleted, or belongs to a different organization; the
    same error if `tenant_id` does not exist or belongs to a different
    organization; and `InvalidTenantRoleError` if `tenant_id` belongs to a
    same-org user whose role is not `TENANT`.
    """
    await PropertyRepository(session, scope).get_or_404(property_id)

    tenant = await UserRepository(session, scope).get_or_404(tenant_id)
    if tenant.role != UserRole.TENANT:
        raise InvalidTenantRoleError("tenant_id must reference a user with role 'tenant'")

    repo = ContractRepository(session, scope)
    contract = repo.add(
        property_id=property_id,
        tenant_id=tenant_id,
        start_date=start_date,
        end_date=end_date,
    )
    await session.flush()
    return contract


async def list_contracts(session: AsyncSession, *, scope: OrgScope) -> list[Contract]:
    """List every contract in `scope`'s organization (via the join through
    `properties`)."""
    repo = ContractRepository(session, scope)
    result = await session.execute(repo.select())
    return list(result.scalars().all())


async def get_contract(
    session: AsyncSession, *, scope: OrgScope, contract_id: UUID
) -> Contract:
    """Fetch a single contract in `scope`'s organization. Raises
    `app.core.exceptions.NotFoundError` if it does not exist or its
    property belongs to a different organization."""
    return await ContractRepository(session, scope).get_or_404(contract_id)


async def update_contract(
    session: AsyncSession, *, scope: OrgScope, contract_id: UUID, **fields: object
) -> Contract:
    """Update `status` on a contract in `scope`'s organization. Start/end
    dates and the parties are immutable — the API layer only ever forwards
    `status` here, but this stays permissive like the other `update_*`
    service functions."""
    return await ContractRepository(session, scope).update(contract_id, **fields)
