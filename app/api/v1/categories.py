"""Org-scoped category CRUD endpoints.

Write access (`POST`/`PATCH`/`DELETE`) is admin-only, via `require_org_admin`.
Read access (`GET`) is available to admins and agents via `require_org_staff`
— categories are reference/taxonomy data, the same assumption Work Unit 5
applied to properties/contracts (see `app.api.deps.require_org_staff`'s
docstring).

`DELETE` always returns 200: the delete-or-deactivate outcome (real delete
vs. soft deactivation because a ticket still references the category) is
reported in the response body, not the status code — see
`app.services.taxonomy_service`.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin, require_org_staff
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.category import Category
from app.schemas.category import (
    CategoryCreate,
    CategoryDeleteResponse,
    CategoryResponse,
    CategoryUpdate,
)
from app.services import category_service
from app.services.category_service import ConflictError
from app.services.taxonomy_service import TaxonomyDeleteResult

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Category:
    """Create a category in the caller's organization. A duplicate name
    (case-insensitive, within the organization) surfaces as 409."""
    try:
        return await category_service.create_category(
            session, scope=scope, name=payload.name, description=payload.description
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    include_inactive: bool = False,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> list[Category]:
    """List categories in the caller's organization. Only `active=true`
    categories are returned unless `include_inactive=true` is passed."""
    return await category_service.list_categories(
        session, scope=scope, include_inactive=include_inactive
    )


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> Category:
    """Fetch a single category in the caller's organization. A cross-org or
    missing id is indistinguishable and surfaces as 404."""
    return await category_service.get_category(session, scope=scope, category_id=category_id)


@router.patch("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: UUID,
    payload: CategoryUpdate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Category:
    """Update `name`/`description`/`active` on a category in the caller's
    organization. A duplicate name (case-insensitive) surfaces as 409."""
    fields = payload.model_dump(exclude_unset=True)
    try:
        return await category_service.update_category(
            session, scope=scope, category_id=category_id, **fields
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/{category_id}", response_model=CategoryDeleteResponse)
async def delete_category(
    category_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> TaxonomyDeleteResult:
    """Delete-or-deactivate a category in the caller's organization —
    always 200; see `app.services.taxonomy_service.delete_or_deactivate`."""
    return await category_service.delete_category(session, scope=scope, category_id=category_id)
