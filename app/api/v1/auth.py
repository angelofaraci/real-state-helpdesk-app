"""Auth endpoints: login, refresh (rotate-on-use), logout, me."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_principal
from app.core.session import get_session
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenPairResponse,
)
from app.services import auth_service
from app.services.auth_service import AuthenticationError, InviteAcceptError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _unauthorized(exc: AuthenticationError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))


def _bad_request(exc: InviteAcceptError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    try:
        pair = await auth_service.login(
            session, email=payload.email, password=payload.password
        )
    except AuthenticationError as exc:
        raise _unauthorized(exc) from exc
    return TokenPairResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    try:
        pair = await auth_service.refresh(session, raw_refresh_token=payload.refresh_token)
    except AuthenticationError as exc:
        raise _unauthorized(exc) from exc
    return TokenPairResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> None:
    await auth_service.logout(session, raw_refresh_token=payload.refresh_token)


@router.post("/accept-invite", response_model=TokenPairResponse)
async def accept_invite(
    payload: AcceptInviteRequest, session: AsyncSession = Depends(get_session)
) -> TokenPairResponse:
    """Public endpoint: activate a pending user via their invite token and
    set their initial password. No bearer token required."""
    try:
        pair = await auth_service.accept_invite(
            session, raw_token=payload.token, new_password=payload.new_password
        )
    except InviteAcceptError as exc:
        raise _bad_request(exc) from exc
    return TokenPairResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.get("/me", response_model=MeResponse)
async def me(principal: User = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        id=principal.id,
        organization_id=principal.organization_id,
        name=principal.name,
        email=principal.email,
        role=principal.role.value,
        status=principal.status.value,
    )
