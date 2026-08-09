"""Category CRUD service (org-scoped).

Authorization is enforced at the API layer (`app.api.deps.require_org_admin`
for writes, `require_org_staff` for reads), not here — this module only
handles persistence, filtering, and conflict mapping.

`delete_category` delegates to the shared
`app.services.taxonomy_service.delete_or_deactivate` helper — see that
module's docstring for the SAVEPOINT/`IntegrityError` fallback logic.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.category import Category
from app.repositories.category_repository import CategoryRepository
from app.services.taxonomy_service import TaxonomyDeleteResult, delete_or_deactivate


class ConflictError(Exception):
    """Raised when the requested category name collides case-insensitively
    with an existing category's name in the same organization (enforced by
    `ix_categories_organization_id_name_lower_unique`)."""


async def create_category(
    session: AsyncSession, *, scope: OrgScope, name: str, description: str | None = None
) -> Category:
    """Create a category in `scope`'s organization. Raises `ConflictError`
    if a category with the same name (case-insensitive) already exists in
    that organization."""
    repo = CategoryRepository(session, scope)
    category = repo.add(name=name, description=description)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"a category named '{name}' already exists") from exc
    return category


async def list_categories(
    session: AsyncSession, *, scope: OrgScope, include_inactive: bool = False
) -> list[Category]:
    """List categories in `scope`'s organization. Only `active=true`
    categories are returned by default; pass `include_inactive=True` to see
    every category regardless of `active`."""
    repo = CategoryRepository(session, scope)
    stmt = repo.list(active=None if include_inactive else True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_category(
    session: AsyncSession, *, scope: OrgScope, category_id: UUID
) -> Category:
    """Fetch a single category in `scope`'s organization. Raises
    `app.core.exceptions.NotFoundError` if it does not exist or belongs to
    a different organization."""
    return await CategoryRepository(session, scope).get_or_404(category_id)


async def update_category(
    session: AsyncSession, *, scope: OrgScope, category_id: UUID, **fields: object
) -> Category:
    """Update `name`/`description`/`active` on a category in `scope`'s
    organization. Raises `ConflictError` if the new name collides
    (case-insensitive) with another category's name in the same
    organization."""
    repo = CategoryRepository(session, scope)
    category = await repo.update(category_id, **fields)
    try:
        await session.flush()
    except IntegrityError as exc:
        name = fields.get("name")
        raise ConflictError(f"a category named '{name}' already exists") from exc
    return category


async def delete_category(
    session: AsyncSession, *, scope: OrgScope, category_id: UUID
) -> TaxonomyDeleteResult:
    """Delete-or-deactivate a category in `scope`'s organization — see
    `app.services.taxonomy_service.delete_or_deactivate`."""
    repo = CategoryRepository(session, scope)
    return await delete_or_deactivate(session, repo=repo, id_=category_id)
