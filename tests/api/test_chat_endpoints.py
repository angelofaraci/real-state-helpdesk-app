"""API-level tests for `/api/v1/chat/*` (stage 4 — chatbot, PR5 — API
wiring). No live Postgres is available in this sandbox, so `get_session` is
overridden with an `AsyncMock` and the relevant repository/service calls
are monkeypatched, following the exact pattern established by
`tests/api/test_messages_endpoints.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps_chat, deps_rag
from app.core.session import get_session
from app.main import app
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import ChatMessageRole, ChatSessionStatus
from app.services import chat as chat_service
from app.services.chat_rate_limit import ChatRateLimitExceededError


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    app.dependency_overrides[deps_rag.get_rag_embedder] = lambda: object()
    app.dependency_overrides[deps_rag.get_llm_client] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


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


def _chat_message(chat_session_id, role=ChatMessageRole.USER, content="hi", **overrides) -> ChatMessage:
    defaults = dict(
        id=uuid4(),
        chat_session_id=chat_session_id,
        role=role,
        content=content,
        tool_name="some_tool" if role == ChatMessageRole.TOOL else None,
        tool_call_id="call_1" if role == ChatMessageRole.TOOL else None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ChatMessage(**defaults)


def _override_chat_session_scope(chat_session, scope=None, is_anonymous=True):
    from app.core.scope import OrgScope

    resolved_scope = scope or OrgScope.for_anonymous_chat(chat_session.organization_id)
    principal = deps_chat.ChatPrincipal(
        scope=resolved_scope, chat_session=chat_session, is_anonymous=is_anonymous
    )
    app.dependency_overrides[deps_chat.get_chat_session_scope] = lambda: principal
    return principal


# ---------------------------------------------------------------------------
# 5.6 — POST /sessions with an unrecognized widget_key -> 404.
# ---------------------------------------------------------------------------


def test_post_sessions_unknown_widget_key_404(client) -> None:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    get_session_override = app.dependency_overrides[get_session]
    session = get_session_override()
    session.execute = AsyncMock(return_value=execute_result)
    app.dependency_overrides[get_session] = lambda: session

    response = client.post("/api/v1/chat/sessions", json={"widget_key": "does-not-exist"})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5.7 — 31st message within the rolling hour -> 429, checked BEFORE
# persisting the message or calling the LLM/embedder.
# ---------------------------------------------------------------------------


def test_post_messages_rate_limited_429(client, monkeypatch) -> None:
    chat_session = _chat_session()
    _override_chat_session_scope(chat_session)
    app.dependency_overrides[deps_chat.get_redis] = lambda: object()

    monkeypatch.setattr(
        "app.api.v1.chat.check_and_increment",
        AsyncMock(
            side_effect=ChatRateLimitExceededError(chat_session.id, limit=30, retry_after=120)
        ),
    )
    send_message_mock = AsyncMock()
    monkeypatch.setattr(chat_service, "send_message", send_message_mock)

    response = client.post(
        f"/api/v1/chat/sessions/{chat_session.id}/messages",
        json={"content": "one more message"},
    )

    assert response.status_code == 429
    send_message_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5.3 — message-send path 409s when the session is not ACTIVE.
# ---------------------------------------------------------------------------


def test_message_post_409_when_session_not_active(client, monkeypatch) -> None:
    chat_session = _chat_session(status=ChatSessionStatus.CLOSED)
    _override_chat_session_scope(chat_session)
    app.dependency_overrides[deps_chat.get_redis] = lambda: object()

    check_and_increment_mock = AsyncMock()
    monkeypatch.setattr("app.api.v1.chat.check_and_increment", check_and_increment_mock)
    send_message_mock = AsyncMock()
    monkeypatch.setattr(chat_service, "send_message", send_message_mock)

    response = client.post(
        f"/api/v1/chat/sessions/{chat_session.id}/messages",
        json={"content": "hello"},
    )

    assert response.status_code == 409
    check_and_increment_mock.assert_not_awaited()
    send_message_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5.8 — GET history excludes tool-role rows, returns chronological order.
# ---------------------------------------------------------------------------


def test_get_messages_excludes_tool_rows_chronological(client) -> None:
    chat_session = _chat_session()
    principal = _override_chat_session_scope(chat_session)

    user_msg = _chat_message(chat_session.id, role=ChatMessageRole.USER, content="hi there")
    tool_msg = _chat_message(chat_session.id, role=ChatMessageRole.TOOL, content='{"ok": true}')
    assistant_msg = _chat_message(
        chat_session.id, role=ChatMessageRole.ASSISTANT, content="how can I help?"
    )

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [user_msg, tool_msg, assistant_msg]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    app.dependency_overrides[get_session] = lambda: session

    response = client.get(f"/api/v1/chat/sessions/{chat_session.id}/messages")

    assert response.status_code == 200
    body = response.json()
    assert [item["role"] for item in body] == ["user", "assistant"]
    assert [item["content"] for item in body] == ["hi there", "how can I help?"]
