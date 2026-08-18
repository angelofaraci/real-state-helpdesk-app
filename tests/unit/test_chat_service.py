"""Unit tests for `app.services.chat` (stage 4 — chatbot, PR4 —
orchestration).

`AsyncSession` is mocked and the relevant repository methods are
monkeypatched directly, following the exact pattern established by
`tests/unit/test_chat_tools.py`/`tests/unit/test_rag_service.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import ChatMessageRole, ChatSessionStatus, UserRole
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.services import chat as chat_service
from app.services import chat_tools


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.TENANT)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _chat_session(scope: OrgScope, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
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
        tool_name=None,
        tool_call_id=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ChatMessage(**defaults)


class _FakeEmbedder:
    def __init__(self, vectors=None) -> None:
        self._vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return self._vectors if self._vectors is not None else [[0.1] * 768 for _ in texts]


def _tool_call(name: str, arguments: dict, call_id: str | None = None) -> MagicMock:
    function = MagicMock()
    function.name = name
    function.arguments = json.dumps(arguments)
    tool_call = MagicMock()
    tool_call.id = call_id or f"call_{name}_{uuid4().hex[:6]}"
    tool_call.function = function
    return tool_call


class _FakeLLMClient:
    """Pops one canned response per `create()` call. Each response is a
    dict: `{"content": str | None, "tool_calls": list | None}`."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

        class _Completions:
            def __init__(self, outer: "_FakeLLMClient") -> None:
                self._outer = outer

            async def create(self, **kwargs):
                self._outer.calls.append(kwargs)
                response = self._outer._responses.pop(0)
                message = MagicMock()
                message.content = response.get("content")
                message.tool_calls = response.get("tool_calls")
                choice = MagicMock(message=message)
                return MagicMock(choices=[choice])

        self.chat = MagicMock(completions=_Completions(self))


def _execute_result(*, scalars_list=None) -> MagicMock:
    result = MagicMock()
    if scalars_list is not None:
        result.scalars.return_value.all.return_value = scalars_list
    return result


def _rows_result(rows: list[tuple[object, float]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _session_returning(*results) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    session.flush = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# 4.1/4.2 — tools= payload matches available_tool_names for the session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_available_tools_anonymous_excludes_gated_tools() -> None:
    scope = OrgScope.for_anonymous_chat(uuid4())
    chat_session = _chat_session(scope, user_id=None)
    session = _session_returning(
        _execute_result(scalars_list=[]),  # history
        _rows_result([]),  # kb search — no chunks -> low confidence
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient([{"content": "Sure, how can I help?", "tool_calls": None}])

    await chat_service.send_message(
        session,
        scope=scope,
        chat_session=chat_session,
        content="hello",
        embedder=embedder,
        llm_client=llm_client,
    )

    assert len(llm_client.calls) == 1
    tool_schemas = llm_client.calls[0]["tools"]
    schema_names = {schema["function"]["name"] for schema in tool_schemas}
    assert schema_names == {"escalate_to_human"}


@pytest.mark.asyncio
async def test_available_tools_identified_offers_all_four() -> None:
    scope = _scope()
    chat_session = _chat_session(scope)
    session = _session_returning(
        _execute_result(scalars_list=[]),
        _rows_result([]),
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient([{"content": "Sure, how can I help?", "tool_calls": None}])

    await chat_service.send_message(
        session,
        scope=scope,
        chat_session=chat_session,
        content="hello",
        embedder=embedder,
        llm_client=llm_client,
    )

    tool_schemas = llm_client.calls[0]["tools"]
    schema_names = {schema["function"]["name"] for schema in tool_schemas}
    assert schema_names == {
        "check_ticket_status",
        "create_ticket",
        "schedule_visit",
        "escalate_to_human",
    }


# ---------------------------------------------------------------------------
# 4.3 — retrieval uses KnowledgeChunkRepository only, never ticket embeddings.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieval_uses_knowledge_chunks_only(monkeypatch) -> None:
    scope = _scope()
    chat_session = _chat_session(scope)
    session = _session_returning(
        _execute_result(scalars_list=[]),
        _rows_result([]),
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient([{"content": "ok", "tool_calls": None}])

    search_spy = AsyncMock(wraps=None)
    original_search = KnowledgeChunkRepository.search
    calls: list[tuple] = []

    def _spy_search(self, embedding, **kwargs):
        calls.append((embedding, kwargs))
        return original_search(self, embedding, **kwargs)

    monkeypatch.setattr(KnowledgeChunkRepository, "search", _spy_search)

    await chat_service.send_message(
        session,
        scope=scope,
        chat_session=chat_session,
        content="hello",
        embedder=embedder,
        llm_client=llm_client,
    )

    assert len(calls) == 1
    assert "ticket_embedding_repository" not in dir(chat_service)
    assert "TicketEmbeddingRepository" not in vars(chat_service)


# ---------------------------------------------------------------------------
# 4.4/4.5 — low-confidence streak escalation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_streak_two_consecutive_triggers_escalation(monkeypatch) -> None:
    scope = _scope()
    chat_session = _chat_session(scope, low_confidence_streak=0)
    embedder = _FakeEmbedder()
    escalate_mock = AsyncMock(return_value={"escalated": True, "ticket_id": None})
    monkeypatch.setattr(chat_tools, "escalate_to_human", escalate_mock)

    # Turn 1: zero chunks -> streak becomes 1, no escalation yet.
    session1 = _session_returning(
        _execute_result(scalars_list=[]),
        _rows_result([]),
    )
    llm_client1 = _FakeLLMClient([{"content": "Let me check.", "tool_calls": None}])
    result1 = await chat_service.send_message(
        session1,
        scope=scope,
        chat_session=chat_session,
        content="my issue",
        embedder=embedder,
        llm_client=llm_client1,
    )
    assert chat_session.low_confidence_streak == 1
    assert result1.escalated is False
    escalate_mock.assert_not_awaited()

    # Turn 2: zero chunks again -> streak becomes 2 -> auto-escalation.
    session2 = _session_returning(
        _execute_result(scalars_list=[]),
        _rows_result([]),
    )
    llm_client2 = _FakeLLMClient([{"content": "Still checking.", "tool_calls": None}])
    result2 = await chat_service.send_message(
        session2,
        scope=scope,
        chat_session=chat_session,
        content="still stuck",
        embedder=embedder,
        llm_client=llm_client2,
    )

    escalate_mock.assert_awaited_once()
    assert result2.escalated is True


@pytest.mark.asyncio
async def test_low_confidence_streak_resets_on_non_consecutive(monkeypatch) -> None:
    scope = _scope()
    chat_session = _chat_session(scope, low_confidence_streak=0)
    embedder = _FakeEmbedder()
    escalate_mock = AsyncMock(return_value={"escalated": True, "ticket_id": None})
    monkeypatch.setattr(chat_tools, "escalate_to_human", escalate_mock)

    # Turn 1: low confidence -> streak = 1.
    session1 = _session_returning(_execute_result(scalars_list=[]), _rows_result([]))
    llm_client1 = _FakeLLMClient([{"content": "a", "tool_calls": None}])
    await chat_service.send_message(
        session1,
        scope=scope,
        chat_session=chat_session,
        content="q1",
        embedder=embedder,
        llm_client=llm_client1,
    )
    assert chat_session.low_confidence_streak == 1

    # Turn 2: high confidence (a chunk above threshold) -> streak resets to 0.
    chunk = MagicMock(content="some kb content")
    session2 = _session_returning(_execute_result(scalars_list=[]), _rows_result([(chunk, 0.9)]))
    llm_client2 = _FakeLLMClient([{"content": "b", "tool_calls": None}])
    await chat_service.send_message(
        session2,
        scope=scope,
        chat_session=chat_session,
        content="q2",
        embedder=embedder,
        llm_client=llm_client2,
    )
    assert chat_session.low_confidence_streak == 0

    # Turn 3: low confidence again -> streak = 1 only (not 2), no escalation.
    session3 = _session_returning(_execute_result(scalars_list=[]), _rows_result([]))
    llm_client3 = _FakeLLMClient([{"content": "c", "tool_calls": None}])
    result3 = await chat_service.send_message(
        session3,
        scope=scope,
        chat_session=chat_session,
        content="q3",
        embedder=embedder,
        llm_client=llm_client3,
    )

    assert chat_session.low_confidence_streak == 1
    assert result3.escalated is False
    escalate_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4.6 — tool loop hard cap forces a final text answer with `tools` absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_loop_hard_cap_forces_final_text_answer(monkeypatch) -> None:
    scope = _scope()
    chat_session = _chat_session(scope)
    session = _session_returning(
        _execute_result(scalars_list=[]),
        _rows_result([(MagicMock(content="kb"), 0.9)]),
    )
    embedder = _FakeEmbedder()

    settings = get_settings()
    cap = settings.chat_max_tool_rounds

    escalate_call = _tool_call("escalate_to_human", {"reason": "stuck"})
    responses = [{"content": None, "tool_calls": [escalate_call]} for _ in range(cap)]
    responses.append({"content": "Here is your final answer.", "tool_calls": [escalate_call]})
    llm_client = _FakeLLMClient(responses)

    escalate_mock = AsyncMock(return_value={"escalated": True, "ticket_id": None})
    monkeypatch.setattr(chat_tools, "_TOOL_HANDLERS", {**chat_tools._TOOL_HANDLERS, "escalate_to_human": escalate_mock})

    result = await chat_service.send_message(
        session,
        scope=scope,
        chat_session=chat_session,
        content="help",
        embedder=embedder,
        llm_client=llm_client,
    )

    assert len(llm_client.calls) == cap + 1
    for call in llm_client.calls[:cap]:
        assert "tools" in call
    final_call = llm_client.calls[-1]
    assert "tools" not in final_call
    assert result.reply == "Here is your final answer."
