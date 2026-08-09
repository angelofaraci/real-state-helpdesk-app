"""Org-scoped property CRUD endpoints.

Write access (`POST`/`PATCH`/`DELETE`) is admin-only, via `require_org_admin`.
Read access (`GET`) is available to admins and agents via `require_org_staff`
— property browsing is treated as a staff concept in this stage, not a
tenant/owner one (see `app.api.deps.require_org_staff`'s docstring for the
stated assumption).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin, require_org_staff
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.property import Property
from app.schemas.property import PropertyCreate, PropertyResponse, PropertyUpdate
from app.services import property_service
from app.services.property_service import InvalidOwnerRoleError

router = APIRouter(prefix="/api/v1/properties", tags=["properties"])


@router.post("", response_model=PropertyResponse, status_code=status.HTTP_201_CREATED)
async def create_property(
    payload: PropertyCreate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Property:
    """Create a property in the caller's organization, owned by
    `payload.owner_id`. A cross-org `owner_id` is indistinguishable from a
    missing one and surfaces as 404; a same-org `owner_id` with the wrong
    role surfaces as 422."""
    try:
        return await property_service.create_property(
            session,
            scope=scope,
            owner_id=payload.owner_id,
            address=payload.address,
            type=payload.type,
        )
    except InvalidOwnerRoleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("", response_model=list[PropertyResponse])
async def list_properties(
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> list[Property]:
    """List every non-soft-deleted property in the caller's organization."""
    return await property_service.list_properties(session, scope=scope)


@router.get("/{property_id}", response_model=PropertyResponse)
async def get_property(
    property_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> Property:
    """Fetch a single property in the caller's organization. A cross-org or
    soft-deleted id is indistinguishable from a missing one and surfaces as
    404."""
    return await property_service.get_property(session, scope=scope, property_id=property_id)


@router.patch("/{property_id}", response_model=PropertyResponse)
async def update_property(
    property_id: UUID,
    payload: PropertyUpdate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Property:
    """Update `address`/`type` on a property in the caller's organization."""
    fields = payload.model_dump(exclude_unset=True)
    return await property_service.update_property(
        session, scope=scope, property_id=property_id, **fields
    )


@router.delete("/{property_id}", response_model=PropertyResponse)
async def delete_property(
    property_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> Property:
    """Soft-delete a property in the caller's organization: sets
    `deleted_at` without removing the row."""
    return await property_service.delete_property(session, scope=scope, property_id=property_id)
