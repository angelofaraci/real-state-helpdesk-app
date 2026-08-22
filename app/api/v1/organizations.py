"""Organization CRUD endpoints — super-admin only.

`require_super_admin` is wired at the router level (`dependencies=[...]`),
not per-route, so it is structurally impossible to add a route to this file
without the check.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.core.session import get_session
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)
from app.services import organization_service
from app.services.organization_service import ConflictError

router = APIRouter(
    prefix="/api/v1/organizations",
    tags=["organizations"],
    dependencies=[Depends(require_super_admin)],
)


@router.post("", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    session: AsyncSession = Depends(get_session),
    principal: User = Depends(require_super_admin),
) -> Organization:
    try:
        return await organization_service.create_organization(
            session,
            name=payload.name,
            actor=principal,
            timezone=payload.timezone,
            business_hours=payload.business_hours,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    organization_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Organization:
    """Cross-org access is not a concept here (super-admin sees every
    organization); an unknown id still surfaces as 404 via `NotFoundError`,
    handled by the app-wide exception handler."""
    return await organization_service.get_organization(session, organization_id=organization_id)


@router.patch("/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: UUID,
    payload: OrganizationUpdate,
    session: AsyncSession = Depends(get_session),
) -> Organization:
    fields = payload.model_dump(exclude_unset=True)
    try:
        return await organization_service.update_organization(
            session, organization_id=organization_id, **fields
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
