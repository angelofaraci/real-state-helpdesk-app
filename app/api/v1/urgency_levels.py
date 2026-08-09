"""Org-scoped urgency-level CRUD endpoints. Mirrors
`app.api.v1.categories` — same role gates and delete-or-deactivate
behavior.

The `DELETE` route here was not in the original brief; it was CONFIRMED
during this SDD cycle specifically to make urgency-levels consistent with
categories' delete-or-deactivate rule (both are referenced by `Ticket` via
a `RESTRICT` FK), not a scope error.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin, require_org_staff
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.urgency_level import UrgencyLevel
from app.schemas.urgency_level import (
    UrgencyLevelCreate,
    UrgencyLevelDeleteResponse,
    UrgencyLevelResponse,
    UrgencyLevelUpdate,
)
from app.services import urgency_level_service
from app.services.taxonomy_service import TaxonomyDeleteResult
from app.services.urgency_level_service import ConflictError

router = APIRouter(prefix="/api/v1/urgency-levels", tags=["urgency-levels"])


@router.post("", response_model=UrgencyLevelResponse, status_code=status.HTTP_201_CREATED)
async def create_urgency_level(
    payload: UrgencyLevelCreate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> UrgencyLevel:
    """Create an urgency level in the caller's organization. A duplicate
    name (case-insensitive, within the organization) surfaces as 409."""
    try:
        return await urgency_level_service.create_urgency_level(
            session,
            scope=scope,
            name=payload.name,
            sla_hours=payload.sla_hours,
            sort_order=payload.sort_order,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("", response_model=list[UrgencyLevelResponse])
async def list_urgency_levels(
    include_inactive: bool = False,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> list[UrgencyLevel]:
    """List urgency levels in the caller's organization, ordered by
    `sort_order`. Only `active=true` levels are returned unless
    `include_inactive=true` is passed."""
    return await urgency_level_service.list_urgency_levels(
        session, scope=scope, include_inactive=include_inactive
    )


@router.get("/{urgency_level_id}", response_model=UrgencyLevelResponse)
async def get_urgency_level(
    urgency_level_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> UrgencyLevel:
    """Fetch a single urgency level in the caller's organization. A
    cross-org or missing id is indistinguishable and surfaces as 404."""
    return await urgency_level_service.get_urgency_level(
        session, scope=scope, urgency_level_id=urgency_level_id
    )


@router.patch("/{urgency_level_id}", response_model=UrgencyLevelResponse)
async def update_urgency_level(
    urgency_level_id: UUID,
    payload: UrgencyLevelUpdate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> UrgencyLevel:
    """Update `name`/`sla_hours`/`sort_order`/`active` on an urgency level
    in the caller's organization. A duplicate name (case-insensitive)
    surfaces as 409."""
    fields = payload.model_dump(exclude_unset=True)
    try:
        return await urgency_level_service.update_urgency_level(
            session, scope=scope, urgency_level_id=urgency_level_id, **fields
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/{urgency_level_id}", response_model=UrgencyLevelDeleteResponse)
async def delete_urgency_level(
    urgency_level_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> TaxonomyDeleteResult:
    """Delete-or-deactivate an urgency level in the caller's organization —
    always 200; see `app.services.taxonomy_service.delete_or_deactivate`."""
    return await urgency_level_service.delete_urgency_level(
        session, scope=scope, urgency_level_id=urgency_level_id
    )
