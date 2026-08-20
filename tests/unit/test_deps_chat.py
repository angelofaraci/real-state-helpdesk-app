"""Unit tests for `app.api.deps_chat.get_chat_session_scope` (stage 4 —
chatbot, PR5 — API wiring).

`AsyncSession` is mocked and the relevant repository/query results are
supplied via `session.execute.side_effect`, following the exact pattern
established by `tests/unit/test_chat_service.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import deps_chat
from app.core.chat_token import encode_chat_session_token
from app.core.exceptions import NotFoundError
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, UserRole, UserStatus


def _fake_user(*, organization_id, role=UserRole.TENANT, status=UserStatus.ACTIVE):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": organization_id,
            "role": role,
            "status": status,
        },
    )()


def _chat_session(**overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        user_id=None,
        ticket_id=None,
        status=ChatSessionStatus.ACTIVE,
        low_confidence_streak=0,
        last_activity_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _execute_scalar(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _session_returning(*results) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


@pytest.mark.asyncio
async def test_bearer_wins_over_header_cross_org_404(monkeypatch) -> None:
    """A valid Bearer JWT takes priority over any `X-Chat-Session` header:
    the header is never even decoded when a Bearer token is present."""
    from app.core.jwt import encode_access_token

    bearer_org_id = uuid4()
    user = _fake_user(organization_id=bearer_org_id)
    token = encode_access_token(sub=user.id, org=bearer_org_id, role=user.role.value)

    def _decode_chat_token_should_not_be_called(_token: str):
        raise AssertionError("X-Chat-Session token must not be decoded when Bearer wins")

    monkeypatch.setattr(
        deps_chat, "decode_chat_session_token", _decode_chat_token_should_not_be_called
    )

    session_id = uuid4()
    session = _session_returning(
        _execute_scalar(user),  # user lookup
        _execute_scalar(None),  # chat session lookup — different org, not visible
    )

    with pytest.raises(NotFoundError):
        await deps_chat.get_chat_session_scope(
            session_id=session_id,
            chat_token="garbage-not-a-real-token",
            authorization=f"Bearer {token}",
            session=session,
        )


@pytest.mark.asyncio
async def test_chat_header_sid_mismatch_401() -> None:
    """The `X-Chat-Session` token's `sid` claim must equal the
    `session_id` path parameter; a mismatch is a 401, not a 404."""
    path_session_id = uuid4()
    token_session_id = uuid4()
    token = encode_chat_session_token(
        session_id=token_session_id, organization_id=uuid4(), user_id=None
    )

    session = _session_returning()

    with pytest.raises(HTTPException) as exc_info:
        await deps_chat.get_chat_session_scope(
            session_id=path_session_id,
            chat_token=token,
            authorization=None,
            session=session,
        )

    assert exc_info.value.status_code == 401
