"""Admin-driven user invitation + org-admin user CRUD endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_principal, require_org_admin
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.schemas.user import InviteUserRequest, UserResponse, UserUpdate
from app.services import user_service
from app.services.user_service import ConflictError, ForbiddenError, NotFoundError

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: InviteUserRequest,
    principal: User = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Create a `pending` user in the caller's organization and email them
    an invite link. The raw invite token is never returned in the response
    body — it is only delivered via email."""
    try:
        return await user_service.invite_user(
            session,
            admin=principal,
            name=payload.name,
            email=payload.email,
            role=payload.role,
        )
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a user with this email already exists"
        ) from exc


@router.post("/{user_id}/invite", response_model=UserResponse)
async def reissue_invite(
    user_id: UUID,
    principal: User = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Re-send an invite to a still-`pending` user in the caller's
    organization: deletes any outstanding invite token and issues a fresh
    one."""
    try:
        return await user_service.reissue_invite(session, admin=principal, user_id=user_id)
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("", response_model=list[UserResponse])
async def list_users(
    role: UserRole | None = None,
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> list[User]:
    """List every user in the caller's organization, optionally filtered by
    `?role=` and/or `?status=`."""
    return await user_service.list_users(session, scope=scope, role=role, status=status_filter)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Fetch a single user in the caller's organization. A cross-org id is
    indistinguishable from a missing one and surfaces as 404."""
    return await user_service.get_user(session, scope=scope, user_id=user_id)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Update `name`/`role` on a user in the caller's organization. Email
    and password are not editable through this endpoint."""
    fields = payload.model_dump(exclude_unset=True)
    return await user_service.update_user(session, scope=scope, user_id=user_id, **fields)


@router.delete("/{user_id}", response_model=UserResponse)
async def disable_user(
    user_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Soft-delete a user in the caller's organization: sets
    `status = disabled` without removing the row."""
    return await user_service.disable_user(session, scope=scope, user_id=user_id)
