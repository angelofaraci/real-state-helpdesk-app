"""Unit tests for `app.api.deps.get_principal`."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import get_principal
from app.core.jwt import encode_access_token
from app.models.enums import UserStatus
from app.models.user import User


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _session_returning(user) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = user
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_get_principal_returns_active_user_for_valid_token() -> None:
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.status = UserStatus.ACTIVE
    token = encode_access_token(sub=user.id, org=None, role="admin")
    session = _session_returning(user)

    principal = await get_principal(_bearer(token), session)

    assert principal is user


@pytest.mark.asyncio
async def test_get_principal_rejects_invalid_token() -> None:
    session = _session_returning(None)

    with pytest.raises(HTTPException) as exc_info:
        await get_principal(_bearer("not-a-real-jwt"), session)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_principal_rejects_when_user_no_longer_exists() -> None:
    token = encode_access_token(sub=uuid4(), org=None, role="admin")
    session = _session_returning(None)

    with pytest.raises(HTTPException) as exc_info:
        await get_principal(_bearer(token), session)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_principal_rejects_inactive_user() -> None:
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.status = UserStatus.DISABLED
    token = encode_access_token(sub=user.id, org=None, role="admin")
    session = _session_returning(user)

    with pytest.raises(HTTPException) as exc_info:
        await get_principal(_bearer(token), session)

    assert exc_info.value.status_code == 401
