"""Shared FastAPI dependencies for the API layer."""

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt import InvalidAccessTokenError, decode_access_token
from app.core.session import get_session
from app.models.enums import UserStatus
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
