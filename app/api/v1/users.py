"""Admin-driven user invitation endpoints.

Full user CRUD (list/get/patch/delete) is out of scope here; see a later
work unit.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_principal
from app.core.session import get_session
from app.models.user import User
from app.schemas.user import InviteUserRequest, UserResponse
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
