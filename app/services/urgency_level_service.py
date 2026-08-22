"""Urgency-level CRUD service (org-scoped). Mirrors
`app.services.category_service` — same shape, `sla_hours`/`sort_order` in
place of `description`.

`delete_urgency_level` delegates to the shared
`app.services.taxonomy_service.delete_or_deactivate` helper.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.urgency_level import UrgencyLevel
from app.repositories.urgency_level_repository import UrgencyLevelRepository
from app.services.taxonomy_service import TaxonomyDeleteResult, delete_or_deactivate


class ConflictError(Exception):
    """Raised when the requested urgency-level name collides
    case-insensitively with an existing urgency level's name in the same
    organization (enforced by
    `ix_urgency_levels_organization_id_name_lower_unique`)."""


async def create_urgency_level(
    session: AsyncSession,
    *,
    scope: OrgScope,
    name: str,
    sla_hours: int,
    sort_order: int = 0,
    respects_business_hours: bool = True,
) -> UrgencyLevel:
    """Create an urgency level in `scope`'s organization. Raises
    `ConflictError` if an urgency level with the same name (case-insensitive)
    already exists in that organization."""
    repo = UrgencyLevelRepository(session, scope)
    urgency = repo.add(
        name=name,
        sla_hours=sla_hours,
        sort_order=sort_order,
        respects_business_hours=respects_business_hours,
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"an urgency level named '{name}' already exists") from exc
    return urgency


async def list_urgency_levels(
    session: AsyncSession, *, scope: OrgScope, include_inactive: bool = False
) -> list[UrgencyLevel]:
    """List urgency levels in `scope`'s organization, ordered by
    `sort_order`. Only `active=true` levels are returned by default; pass
    `include_inactive=True` to see every level regardless of `active`."""
    repo = UrgencyLevelRepository(session, scope)
    stmt = repo.list(active=None if include_inactive else True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_urgency_level(
    session: AsyncSession, *, scope: OrgScope, urgency_level_id: UUID
) -> UrgencyLevel:
    """Fetch a single urgency level in `scope`'s organization. Raises
    `app.core.exceptions.NotFoundError` if it does not exist or belongs to
    a different organization."""
    return await UrgencyLevelRepository(session, scope).get_or_404(urgency_level_id)


async def update_urgency_level(
    session: AsyncSession, *, scope: OrgScope, urgency_level_id: UUID, **fields: object
) -> UrgencyLevel:
    """Update `name`/`sla_hours`/`sort_order`/`active` on an urgency level
    in `scope`'s organization. Raises `ConflictError` if the new name
    collides (case-insensitive) with another urgency level's name in the
    same organization."""
    repo = UrgencyLevelRepository(session, scope)
    urgency = await repo.update(urgency_level_id, **fields)
    try:
        await session.flush()
    except IntegrityError as exc:
        name = fields.get("name")
        raise ConflictError(f"an urgency level named '{name}' already exists") from exc
    return urgency


async def delete_urgency_level(
    session: AsyncSession, *, scope: OrgScope, urgency_level_id: UUID
) -> TaxonomyDeleteResult:
    """Delete-or-deactivate an urgency level in `scope`'s organization —
    see `app.services.taxonomy_service.delete_or_deactivate`."""
    repo = UrgencyLevelRepository(session, scope)
    return await delete_or_deactivate(session, repo=repo, id_=urgency_level_id)
