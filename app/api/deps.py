"""Shared FastAPI dependencies for the API layer."""

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt import InvalidAccessTokenError, decode_access_token
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import UserRole, UserStatus
from app.models.user import User

_bearer_scheme = HTTPBearer()

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_principal(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the authenticated `User` from a bearer access token.

    Rejects (401) an invalid/expired token, a token for a user that no
    longer exists, or a user that is no longer active.
    """
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidAccessTokenError as exc:
        raise _UNAUTHORIZED from exc

    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise _UNAUTHORIZED from exc

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None or user.status != UserStatus.ACTIVE:
        raise _UNAUTHORIZED

    return user


async def require_super_admin(principal: User = Depends(get_principal)) -> User:
    """Require the acting principal to be a platform-level super-admin
    (`organization_id is None` and `role == ADMIN`).

    Raises 404, not 403, for anyone else — including a regular org admin.
    Per the approved design's status-code precedence, an org-scoped user
    must not be able to distinguish "/organizations exists but I can't use
    it" from "this route doesn't exist", the same existence-hiding rule
    applied to every other cross-scope access in this app.
    """
    if principal.organization_id is not None or principal.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )
    return principal


async def require_org_admin(principal: User = Depends(get_principal)) -> OrgScope:
    """Require the acting principal to be an org-scoped admin, and return
    their `OrgScope`. Raises 403 for any non-admin role and for a
    super-admin (`organization_id is None`), which has no org scope."""
    if principal.role != UserRole.ADMIN or principal.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privileges required",
        )
    return OrgScope.from_principal(principal)
