"""Integration tests for the full chat-widget flow (stage 4 — chatbot,
PR6 — Integration & Cleanup) against a REAL Postgres instance and the
real FastAPI app.

Unlike `tests/api/test_chat_endpoints.py` (which mocks the `AsyncSession`
boundary) and `tests/unit/test_chat_service.py` (which mocks the LLM
client/embedder directly), these tests exercise the actual HTTP routes,
real repositories, and real DB constraints end to end. Only three
dependencies are faked, all at the app's own `Depends(...)` seams:

- `app.api.deps_rag.get_llm_client` — a scripted fake OpenAI-shaped client
  (`FakeLLMClient` below) that returns pre-built completion objects, so
  the test controls exactly which tool (if any) the "model" calls.
- `app.api.deps_rag.get_rag_embedder` — a fake embedder returning a
  constant zero-vector (dimension `RAG_EMBEDDING_DIM`), since no
  `knowledge_chunks` rows exist in this test's org.
- `app.api.deps_chat.get_redis` — the same in-memory `FakeRedis` stand-in
  `tests/unit/test_chat_rate_limit.py` already uses (no real Redis
  dependency is installed on this project — see that module's docstring).

Everything else — `chat_sessions`, `chat_messages`, `tickets`, `messages`,
org/user/property/contract rows — is real Postgres, in the same
SAVEPOINT-scoped `db_session` transaction `tests/conftest.py` sets up, so
nothing written here persists past the test.
"""

from __future__ import annotations

import secrets
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps_chat, deps_rag
from app.core.jwt import encode_access_token
from app.core.session import get_session
from app.main import app
from app.models.contract import Contract
from app.models.enums import (
    AuthorType,
    ContractStatus,
    PropertyType,
    UserRole,
    UserStatus,
)
from app.models.message import Message
from app.models.organization import Organization
from app.models.property import Property
from app.models.ticket import Ticket
from app.models.user import User
from app.services.rag_embeddings import RAG_EMBEDDING_DIM


class _FakePipeline:
    """See `tests/unit/test_chat_rate_limit.py`'s `_FakePipeline` docstring
    — identical minimal duck-typed Redis pipeline stand-in."""

    def __init__(self, store: dict[str, int], ttls: dict[str, int]) -> None:
        self._store = store
        self._ttls = ttls
        self._ops: list[tuple[str, tuple, dict]] = []

    def incr(self, key: str) -> "_FakePipeline":
        self._ops.append(("incr", (key,), {}))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> "_FakePipeline":
        self._ops.append(("expire", (key, seconds), {"nx": nx}))
        return self

    async def execute(self) -> list[int | bool]:
        results: list[int | bool] = []
        for op, args, kwargs in self._ops:
            if op == "incr":
                (key,) = args
                self._store[key] = self._store.get(key, 0) + 1
                results.append(self._store[key])
            elif op == "expire":
                key, seconds = args
                nx = kwargs.get("nx", False)
                if nx and key in self._ttls:
                    results.append(False)
                else:
                    self._ttls[key] = seconds
                    results.append(True)
        self._ops = []
        return results


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, int] = {}
        self._ttls: dict[str, int] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self._store, self._ttls)


class FakeEmbedder:
    """Returns a constant zero-vector for every input — no `knowledge_base`
    rows exist in this test's org, so retrieval is always empty regardless
    of the embedding's actual content."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * RAG_EMBEDDING_DIM for _ in texts]


def _text_completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))]
    )


def _tool_call_completion(name: str, arguments_json: str, call_id: str = "call_1") -> SimpleNamespace:
    tool_call = SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments_json)
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]
    )


class _FakeCompletions:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeLLMClient:
    """Minimal stand-in for `openai.AsyncOpenAI` — only the
    `chat.completions.create(...)` surface `app.services.chat.send_message`
    actually calls."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


async def _make_org(db_session: AsyncSession) -> Organization:
    org = Organization(name=f"Test Org {uuid4()}", chat_widget_key=secrets.token_urlsafe(16))
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_active_user(
    db_session: AsyncSession, org: Organization, *, role: UserRole
) -> User:
    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"{uuid4()}@example.com",
        role=role,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_active_contract(
    db_session: AsyncSession, org: Organization, tenant: User
) -> Contract:
    owner = await _make_active_user(db_session, org, role=UserRole.OWNER)
    prop = Property(
        organization_id=org.id, owner_id=owner.id, address="1 Test St", type=PropertyType.APARTMENT
    )
    db_session.add(prop)
    await db_session.flush()

    contract = Contract(
        property_id=prop.id,
        tenant_id=tenant.id,
        start_date=date(2020, 1, 1),
        end_date=date(2999, 1, 1),
        status=ContractStatus.ACTIVE,
    )
    db_session.add(contract)
    await db_session.flush()
    return contract


@pytest.fixture
def wired_app(db_session: AsyncSession):
    """Override `get_session`/redis/embedder for the duration of one test,
    always leaving the LLM client override to the test itself."""
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[deps_chat.get_redis] = lambda: FakeRedis()
    app.dependency_overrides[deps_rag.get_rag_embedder] = lambda: FakeEmbedder()
    yield app
    app.dependency_overrides.clear()


async def test_anonymous_full_flow(wired_app, db_session: AsyncSession) -> None:
    org = await _make_org(db_session)

    fake_llm = FakeLLMClient([_text_completion("Sure, here is the answer to your question.")])
    wired_app.dependency_overrides[deps_rag.get_llm_client] = lambda: fake_llm

    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/chat/sessions", json={"widget_key": org.chat_widget_key}
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        assert body["is_anonymous"] is True
        session_id = body["id"]
        chat_token = body["chat_token"]

        message_resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "What are your office hours?"},
            headers={"X-Chat-Session": chat_token},
        )

    assert message_resp.status_code == 200
    message_body = message_resp.json()
    assert message_body["reply"] == "Sure, here is the answer to your question."
    assert message_body["ticket_id"] is None
    assert message_body["escalated"] is False
    assert message_body["tools_used"] == []

    # No ticket was created for this anonymous, no-tool-call turn.
    tickets = (await db_session.execute(select(Ticket))).scalars().all()
    assert tickets == []

    # Only `escalate_to_human` was offered to the fake LLM — an anonymous
    # session has no ticket/contract visibility (see
    # `app.services.chat_tools.available_tool_names`'s docstring).
    assert len(fake_llm.completions.calls) == 1
    offered_tool_names = {
        schema["function"]["name"] for schema in fake_llm.completions.calls[0]["tools"]
    }
    assert offered_tool_names == {"escalate_to_human"}


async def test_identified_full_flow(wired_app, db_session: AsyncSession) -> None:
    org = await _make_org(db_session)
    tenant = await _make_active_user(db_session, org, role=UserRole.TENANT)
    await _make_active_contract(db_session, org, tenant)

    access_token = encode_access_token(sub=tenant.id, org=org.id, role=UserRole.TENANT.value)

    fake_llm = FakeLLMClient(
        [
            _tool_call_completion(
                "create_ticket",
                '{"title": "Leaky faucet", "description": "The kitchen faucet is leaking."}',
            ),
            _text_completion("I have created a ticket for you."),
        ]
    )
    wired_app.dependency_overrides[deps_rag.get_llm_client] = lambda: fake_llm

    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/chat/sessions",
            json={"widget_key": org.chat_widget_key},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        assert body["is_anonymous"] is False
        session_id = body["id"]

        first_resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "My kitchen faucet is leaking, please help."},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert first_resp.status_code == 200
        first_body = first_resp.json()
        assert first_body["tools_used"] == ["create_ticket"]
        ticket_id = first_body["ticket_id"]
        assert ticket_id is not None

        # A real `tickets` row exists, and `chat_sessions.ticket_id` is set.
        ticket = (
            await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        assert ticket.organization_id == org.id

        session_stmt = select(Message).where(Message.ticket_id == ticket_id).order_by(
            Message.created_at
        )
        ticket_messages = (await db_session.execute(session_stmt)).scalars().all()
        # The visitor's user-authored chat turn was copied over as
        # `AuthorType.USER`, plus one `AuthorType.BOT` chat-origin note —
        # see `chat_tools._copy_chat_messages_to_ticket`'s docstring.
        assert [m.author_type for m in ticket_messages] == [AuthorType.USER, AuthorType.BOT]
        assert ticket_messages[0].content == "My kitchen faucet is leaking, please help."

        # Second turn: a `schedule_visit` tool call against the now-linked
        # ticket appends a note, with no new table touched.
        fake_llm.completions._responses = [
            _tool_call_completion(
                "schedule_visit",
                f'{{"ticket_id": "{ticket_id}", "preferred_date": "next Tuesday morning"}}',
            ),
            _text_completion("Your visit request has been noted."),
        ]

        second_resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "Can someone visit next Tuesday morning?"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert second_resp.status_code == 200
    second_body = second_resp.json()
    assert second_body["tools_used"] == ["schedule_visit"]
    assert second_body["ticket_id"] == ticket_id

    ticket_messages_after = (
        await db_session.execute(
            select(Message).where(Message.ticket_id == ticket_id).order_by(Message.created_at)
        )
    ).scalars().all()
    assert len(ticket_messages_after) == 3
    assert "next Tuesday morning" in ticket_messages_after[-1].content
    assert ticket_messages_after[-1].author_type == AuthorType.BOT

    # Still exactly one ticket — `schedule_visit` never creates a new one.
    all_tickets = (await db_session.execute(select(Ticket))).scalars().all()
    assert len(all_tickets) == 1
