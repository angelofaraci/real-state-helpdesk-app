"""Core authentication business logic: login, refresh rotation, logout.

Every failure path raises `AuthenticationError` with the SAME message, so
callers can map it to an identical 401 response body regardless of the
underlying reason (unknown email, wrong password, disabled account, ...).
This avoids leaking a user-enumeration signal.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt import encode_access_token
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.enums import UserStatus
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository

_GENERIC_AUTH_FAILURE = "invalid email or password"
_GENERIC_REFRESH_FAILURE = "invalid or expired refresh token"


class AuthenticationError(Exception):
    """Raised for any login/refresh failure. Always carries a generic,
    user-enumeration-safe message."""


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str


async def login(session: AsyncSession, *, email: str, password: str) -> TokenPair:
    """Authenticate by email + password and issue a new access/refresh pair."""
    normalized_email = email.strip().lower()
    stmt = select(User).where(func.lower(User.email) == normalized_email)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None or user.status != UserStatus.ACTIVE or user.password_hash is None:
        # Run a dummy verify so unknown-email / inactive-account paths take
        # a comparable amount of time to a real wrong-password check.
        verify_password(DUMMY_PASSWORD_HASH, password)
        raise AuthenticationError(_GENERIC_AUTH_FAILURE)

    if not verify_password(user.password_hash, password):
        raise AuthenticationError(_GENERIC_AUTH_FAILURE)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    return await _issue_pair(session, user)


async def refresh(session: AsyncSession, *, raw_refresh_token: str) -> TokenPair:
    """Rotate a refresh token on use, detecting reuse of an already-rotated
    (or otherwise revoked) token as a possible theft signal."""
    repo = RefreshTokenRepository(session)
    token_row = await repo.rotate(raw_refresh_token)
    if token_row is None:
        raise AuthenticationError(_GENERIC_REFRESH_FAILURE)

    if token_row.revoked_at is not None:
        await repo.revoke_family(token_row.family_id)
        raise AuthenticationError(_GENERIC_REFRESH_FAILURE)

    if token_row.expires_at < datetime.now(UTC):
        raise AuthenticationError(_GENERIC_REFRESH_FAILURE)

    stmt = select(User).where(User.id == token_row.user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or user.status != UserStatus.ACTIVE:
        raise AuthenticationError(_GENERIC_REFRESH_FAILURE)

    await repo.revoke(token_row)
    return await _issue_pair(session, user, family_id=token_row.family_id)


async def logout(session: AsyncSession, *, raw_refresh_token: str) -> None:
    """Revoke the entire token family of the presented refresh token.

    A no-op for an unknown/already-invalid token: logout is idempotent
    and never leaks whether the token was valid.
    """
    repo = RefreshTokenRepository(session)
    token_row = await repo.rotate(raw_refresh_token)
    if token_row is None:
        return
    await repo.revoke_family(token_row.family_id)


async def _issue_pair(
    session: AsyncSession, user: User, *, family_id: UUID | None = None
) -> TokenPair:
    access_token = encode_access_token(
        sub=user.id, org=user.organization_id, role=user.role.value
    )
    repo = RefreshTokenRepository(session)
    raw_refresh_token, _family_id = await repo.issue(user.id, family_id=family_id)
    return TokenPair(access_token=access_token, refresh_token=raw_refresh_token)
