"""Organization CRUD service (super-admin only).

Authorization is enforced at the API layer via `app.api.deps.require_super_admin`,
not here — this module only handles persistence and conflict mapping.
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository


class ConflictError(Exception):
    """Raised when the requested organization name collides
    case-insensitively with an existing organization's name (enforced by
    the `ix_organizations_name_lower_unique` index)."""


async def create_organization(session: AsyncSession, *, name: str) -> Organization:
    """Create a new organization. Raises `ConflictError` if an organization
    with the same name (case-insensitive) already exists."""
    repo = OrganizationRepository(session)
    org = repo.add(name=name)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"an organization named '{name}' already exists") from exc
    return org


async def get_organization(session: AsyncSession, *, organization_id: UUID) -> Organization:
    """Fetch a single organization by id. Raises `NotFoundError` if it does
    not exist."""
    repo = OrganizationRepository(session)
    return await repo.get_or_404(organization_id)


async def update_organization(
    session: AsyncSession, *, organization_id: UUID, **fields: object
) -> Organization:
    """Update an organization's fields. Raises `NotFoundError` if it does
    not exist, or `ConflictError` if the new name collides (case-insensitive)
    with another organization's name."""
    repo = OrganizationRepository(session)
    org = await repo.update(organization_id, **fields)
    try:
        await session.flush()
    except IntegrityError as exc:
        name = fields.get("name")
        raise ConflictError(f"an organization named '{name}' already exists") from exc
    return org
