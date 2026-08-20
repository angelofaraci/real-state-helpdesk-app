"""Tests for `app.workers.whatsapp.process_whatsapp_message` — the async
worker job that turns one already-ingested inbound WhatsApp message into a
chatbot reply (stage 5 — multichannel, PR5 — WhatsApp worker + outbound).

`AsyncSession` is mocked (no live Postgres in this sandbox); `chat.
send_message` is monkeypatched at the module boundary — this module owns
proving the JOB's own control flow (scope bootstrap, the
`ctx["rag_embedder"]` vs `ctx["embedder"]` distinction, commit-before-
enqueue ordering, retry/permanent-failure classification), not re-proving
`chat.send_message`'s own internals (already covered by
`tests/unit/test_chat_service.py`).

THE SINGLE MOST IMPORTANT TEST IN THIS MODULE:
`test_passes_rag_embedder_not_the_classification_embedder` —
`app.workers.settings.on_startup` builds TWO different embedding providers,
`ctx["embedder"]` (stage-2 classification, wrong dimension/model for chat)
and `ctx["rag_embedder"]` (stage-3 RAG, correct for chat retrieval). Passing
the wrong one raises no exception — it silently returns garbage/empty RAG
context. This module asserts the exact object identity passed to
`chat.send_message`'s `embedder=` kwarg.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import arq
import pytest

from app.core.scope import OrgScope
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, TicketChannel, UserRole, UserStatus
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services import chat as chat_service
from app.services.chat import ChatTurnResult
from app.workers import whatsapp as worker


class _SessionContextManager:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory(session: AsyncMock):
    def factory() -> _SessionContextManager:
        return _SessionContextManager(session)

    return factory


def _execute_result(*, scalar: object = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


def _session_returning(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


class _FakeRagEmbedder:
    async def embed(self, texts):
        return [[0.9, 0.9, 0.9] for _ in texts]


class _FakeClassificationEmbedder:
    async def embed(self, texts):
        return [[0.1, 0.1, 0.1] for _ in texts]


def _chat_session(*, organization_id=None, user_id=None, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id or uuid4(),
        user_id=user_id or uuid4(),
        ticket_id=None,
        status=ChatSessionStatus.ACTIVE,
        low_confidence_streak=0,
        last_activity_at=datetime.now(UTC),
        channel=TicketChannel.WHATSAPP,
        channel_metadata={
            "wa_id": "15550001111",
            "phone_number_id": "1234567890",
            "profile_name": "Jane",
            "last_inbound_at": datetime.now(UTC).isoformat(),
            "processed_message_ids": ["wamid.NEW"],
            "deferred_replies": [],
        },
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _user(*, organization_id, **overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        name="Jane Tenant",
        email="wa+15550001111@whatsapp.invalid",
        role=UserRole.TENANT,
        status=UserStatus.PENDING,
        password_hash=None,
    )
    defaults.update(overrides)
    return User(**defaults)


def _turn_result(**overrides) -> ChatTurnResult:
    defaults = dict(reply="We'll take a look!", escalated=False, ticket_id=None, tools_used=[])
    defaults.update(overrides)
    return ChatTurnResult(**defaults)


# ---------------------------------------------------------------------------
# The critical rag_embedder vs embedder distinction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_rag_embedder_not_the_classification_embedder(monkeypatch) -> None:
    org_id = uuid4()
    chat_session = _chat_session(organization_id=org_id)
    user = _user(organization_id=org_id, id=chat_session.user_id)
    session = _session_returning(_execute_result(scalar=chat_session))
    monkeypatch.setattr(UserRepository, "get_or_404", AsyncMock(return_value=user))

    fake_send_message = AsyncMock(return_value=_turn_result())
    monkeypatch.setattr(chat_service, "send_message", fake_send_message)
    fake_create_pool = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    rag_embedder = _FakeRagEmbedder()
    classification_embedder = _FakeClassificationEmbedder()
    ctx = {
        "session_factory": _session_factory(session),
        "embedder": classification_embedder,
        "rag_embedder": rag_embedder,
        "llm_client": AsyncMock(),
    }

    await worker.process_whatsapp_message(ctx, chat_session.id, "wamid.NEW", "hello there")

    fake_send_message.assert_awaited_once()
    _, kwargs = fake_send_message.call_args
    assert kwargs["embedder"] is rag_embedder
    assert kwargs["embedder"] is not classification_embedder


# ---------------------------------------------------------------------------
# Scope bootstrap: for_background_worker to read, from_principal to write.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_reads_via_background_worker_scope_then_from_principal(
    monkeypatch,
) -> None:
    org_id = uuid4()
    chat_session = _chat_session(organization_id=org_id)
    user = _user(organization_id=org_id, id=chat_session.user_id, role=UserRole.TENANT)
    session = _session_returning(_execute_result(scalar=chat_session))

    captured_scopes: list[OrgScope] = []

    async def _fake_get_or_404(self, id_, **kwargs):
        captured_scopes.append(self._scope)
        assert self._scope.organization_id == org_id
        return user

    monkeypatch.setattr(UserRepository, "get_or_404", _fake_get_or_404)

    fake_send_message = AsyncMock(return_value=_turn_result())
    monkeypatch.setattr(chat_service, "send_message", fake_send_message)
    monkeypatch.setattr(worker, "create_pool", AsyncMock(return_value=AsyncMock()))

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeClassificationEmbedder(),
        "rag_embedder": _FakeRagEmbedder(),
        "llm_client": AsyncMock(),
    }

    await worker.process_whatsapp_message(ctx, chat_session.id, "wamid.NEW", "hello")

    # The bootstrap lookup used a background-worker (system) scope.
    assert len(captured_scopes) == 1

    # The actual chat turn runs under `OrgScope.from_principal(user)`.
    _, kwargs = fake_send_message.call_args
    write_scope = kwargs["scope"]
    assert write_scope.organization_id == org_id
    assert write_scope.user_id == user.id
    assert write_scope.role == UserRole.TENANT


# ---------------------------------------------------------------------------
# Commit-before-enqueue ordering.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commits_before_enqueueing_the_reply_job(monkeypatch) -> None:
    org_id = uuid4()
    chat_session = _chat_session(organization_id=org_id)
    user = _user(organization_id=org_id, id=chat_session.user_id)
    session = _session_returning(_execute_result(scalar=chat_session))
    monkeypatch.setattr(UserRepository, "get_or_404", AsyncMock(return_value=user))
    monkeypatch.setattr(
        chat_service, "send_message", AsyncMock(return_value=_turn_result(reply="hi back"))
    )

    call_order: list[str] = []
    session.commit.side_effect = lambda: call_order.append("commit")
    fake_redis = AsyncMock()
    fake_redis.enqueue_job.side_effect = lambda *a, **k: call_order.append("enqueue")
    monkeypatch.setattr(worker, "create_pool", AsyncMock(return_value=fake_redis))

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeClassificationEmbedder(),
        "rag_embedder": _FakeRagEmbedder(),
        "llm_client": AsyncMock(),
    }

    await worker.process_whatsapp_message(ctx, chat_session.id, "wamid.NEW", "hello")

    assert call_order == ["commit", "enqueue"]
    fake_redis.enqueue_job.assert_awaited_once_with(
        "send_whatsapp_reply", chat_session.id, "hi back"
    )
    fake_redis.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Chat session not found — permanent, no retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_session_not_found_is_permanent_and_does_not_raise(monkeypatch) -> None:
    session = _session_returning(_execute_result(scalar=None))
    fake_send_message = AsyncMock()
    monkeypatch.setattr(chat_service, "send_message", fake_send_message)
    fake_create_pool = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeClassificationEmbedder(),
        "rag_embedder": _FakeRagEmbedder(),
        "llm_client": AsyncMock(),
    }

    await worker.process_whatsapp_message(ctx, uuid4(), "wamid.NEW", "hello")

    fake_send_message.assert_not_awaited()
    fake_create_pool.assert_not_awaited()


# ---------------------------------------------------------------------------
# Transient vs permanent failure classification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_raises_arq_retry_with_backoff(monkeypatch) -> None:
    org_id = uuid4()
    chat_session = _chat_session(organization_id=org_id)
    user = _user(organization_id=org_id, id=chat_session.user_id)
    session = _session_returning(_execute_result(scalar=chat_session))
    monkeypatch.setattr(UserRepository, "get_or_404", AsyncMock(return_value=user))
    monkeypatch.setattr(
        chat_service, "send_message", AsyncMock(side_effect=ConnectionError("network blip"))
    )
    fake_create_pool = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeClassificationEmbedder(),
        "rag_embedder": _FakeRagEmbedder(),
        "llm_client": AsyncMock(),
        "job_try": 2,
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.process_whatsapp_message(ctx, chat_session.id, "wamid.NEW", "hello")

    assert exc_info.value.defer_score == (2**2) * 1000
    session.commit.assert_not_awaited()
    fake_create_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_failure_is_logged_and_swallowed(monkeypatch) -> None:
    org_id = uuid4()
    chat_session = _chat_session(organization_id=org_id)
    user = _user(organization_id=org_id, id=chat_session.user_id)
    session = _session_returning(_execute_result(scalar=chat_session))
    monkeypatch.setattr(UserRepository, "get_or_404", AsyncMock(return_value=user))
    monkeypatch.setattr(
        chat_service, "send_message", AsyncMock(side_effect=ValueError("bad state"))
    )
    fake_create_pool = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    ctx = {
        "session_factory": _session_factory(session),
        "embedder": _FakeClassificationEmbedder(),
        "rag_embedder": _FakeRagEmbedder(),
        "llm_client": AsyncMock(),
    }

    # Must not raise.
    await worker.process_whatsapp_message(ctx, chat_session.id, "wamid.NEW", "hello")

    session.commit.assert_not_awaited()
    fake_create_pool.assert_not_awaited()
