"""Property service: org-scoped property CRUD.

`create_property` validates the referenced `owner_id` through
`UserRepository.get_or_404` — a cross-org or missing owner id surfaces as
`app.core.exceptions.NotFoundError` (404), the same existence-hiding rule
applied everywhere else. A same-org user referenced by `owner_id` whose
role is not `OWNER` is a business-rule validation failure on an
otherwise-visible resource, not a scope violation, so it raises
`InvalidOwnerRoleError` (422) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.enums import PropertyType, UserRole
from app.models.property import Property
from app.repositories.property_repository import PropertyRepository
from app.repositories.user_repository import UserRepository


class PropertyServiceError(Exception):
    """Base class for property service failures."""


class InvalidOwnerRoleError(PropertyServiceError):
    """Raised when `owner_id` refers to a same-org user whose role is not
    `OWNER`."""


async def create_property(
    session: AsyncSession,
    *,
    scope: OrgScope,
    owner_id: UUID,
    address: str,
    type: PropertyType,  # noqa: A002 — mirrors the model/schema field name
) -> Property:
    """Create a property in `scope`'s organization, owned by `owner_id`.

    Raises `app.core.exceptions.NotFoundError` if `owner_id` does not exist
    or belongs to a different organization, and `InvalidOwnerRoleError` if
    it belongs to a same-org user whose role is not `OWNER`.
    """
    owner = await UserRepository(session, scope).get_or_404(owner_id)
    if owner.role != UserRole.OWNER:
        raise InvalidOwnerRoleError("owner_id must reference a user with role 'owner'")

    repo = PropertyRepository(session, scope)
    property_ = repo.add(owner_id=owner_id, address=address, type=type)
    await session.flush()
    return property_


async def list_properties(session: AsyncSession, *, scope: OrgScope) -> list[Property]:
    """List every non-soft-deleted property in `scope`'s organization."""
    repo = PropertyRepository(session, scope)
    result = await session.execute(repo.select())
    return list(result.scalars().all())


async def get_property(
    session: AsyncSession, *, scope: OrgScope, property_id: UUID
) -> Property:
    """Fetch a single property in `scope`'s organization. Raises
    `app.core.exceptions.NotFoundError` if it does not exist, belongs to a
    different organization, or is soft-deleted."""
    return await PropertyRepository(session, scope).get_or_404(property_id)


async def update_property(
    session: AsyncSession, *, scope: OrgScope, property_id: UUID, **fields: object
) -> Property:
    """Update `address`/`type` on a property in `scope`'s organization."""
    return await PropertyRepository(session, scope).update(property_id, **fields)


async def delete_property(
    session: AsyncSession, *, scope: OrgScope, property_id: UUID
) -> Property:
    """Soft-delete a property in `scope`'s organization by setting
    `deleted_at` — the row is never actually deleted."""
    return await PropertyRepository(session, scope).update(
        property_id, deleted_at=datetime.now(UTC)
    )
