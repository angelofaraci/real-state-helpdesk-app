"""Unit tests for `app.services.rag.suggest_response` (Work Unit 5, PR6).

`AsyncSession` is mocked, following the exact pattern established by
`tests/unit/test_ticket_service_update.py`/`tests/unit/test_worker_rag.py`:
a plain `AsyncMock` whose `session.execute` returns a pre-built sequence of
results via `side_effect`. No live Postgres is available in this sandbox
(same constraint noted throughout PR1-5), so tenant isolation (5.4) is
verified the same way `tests/unit/test_knowledge_chunk_repository.py`
verifies `search()` itself: by compiling the exact `Select` statements
`suggest_response` executed and inspecting the resulting SQL for the
scoping organization id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.scope import OrgScope
from app.models.enums import AuthorType, TicketChannel, TicketStatus, UserRole
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.message import Message
from app.models.ticket import Ticket
from app.models.ticket_embedding import TicketEmbedding
from app.services import rag as rag_service
from app.services.rag import SuggestedResponse


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.AGENT)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        title="Leaking faucet",
        description="The kitchen faucet has been dripping for a week.",
        channel=TicketChannel.WEB,
        status=TicketStatus.OPEN,
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _message(*, ticket_id, content="Hello", author_type=AuthorType.USER, **overrides) -> Message:
    defaults = dict(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=author_type,
        content=content,
        is_ai_suggestion=False,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Message(**defaults)


def _suggestion_message(*, ticket_id, created_at=None, **overrides) -> Message:
    defaults = dict(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.BOT,
        content="Here is a suggested reply.",
        is_ai_suggestion=True,
        created_at=created_at or datetime.now(UTC),
    )
    defaults.update(overrides)
    return Message(**defaults)


def _knowledge_chunk(*, organization_id, content="Pets allowed with deposit.", **overrides) -> KnowledgeChunk:
    defaults = dict(
        id=uuid4(),
        knowledge_base_id=uuid4(),
        organization_id=organization_id,
        chunk_index=0,
        content=content,
        token_count=10,
        embedding=[0.1] * 768,
    )
    defaults.update(overrides)
    return KnowledgeChunk(**defaults)


def _ticket_embedding(*, organization_id, summary="A similar past ticket was resolved by X.", **overrides) -> TicketEmbedding:
    defaults = dict(
        id=uuid4(),
        ticket_id=uuid4(),
        organization_id=organization_id,
        summary=summary,
        embedding=[0.1] * 768,
        model_used="text-embedding-3-small",
    )
    defaults.update(overrides)
    return TicketEmbedding(**defaults)


class _FakeEmbedder:
    def __init__(self, vectors=None) -> None:
        self._vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return self._vectors if self._vectors is not None else [[0.1] * 768 for _ in texts]


class _FakeLLMClient:
    def __init__(self, content: str | None = "Try replacing the washer.") -> None:
        self._content = content
        self.calls: list[dict] = []

        class _Completions:
            def __init__(self, outer: "_FakeLLMClient") -> None:
                self._outer = outer

            async def create(self, **kwargs):
                self._outer.calls.append(kwargs)
                message = MagicMock(content=self._outer._content)
                choice = MagicMock(message=message)
                return MagicMock(choices=[choice])

        self.chat = MagicMock(completions=_Completions(self))


def _execute_result(*, scalar=None, scalars_list=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalar_one.return_value = scalar
    if scalars_list is not None:
        result.scalars.return_value.all.return_value = scalars_list
    return result


def _rows_result(rows: list[tuple[object, float]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _session_returning(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


# ---------------------------------------------------------------------------
# 5.1 — cache hit skips regeneration when (created_at, id) unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_returns_existing_suggestion_without_regenerating() -> None:
    scope = _scope()
    ticket = _ticket(scope)
    cached = _suggestion_message(ticket_id=ticket.id)

    session = _session_returning(
        _execute_result(scalar=ticket),  # unlocked get_or_404
        _execute_result(scalar=cached),  # _latest_suggestion
        _execute_result(scalar=False),  # _is_fresh: no superseding message -> fresh
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient()

    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    assert isinstance(result, SuggestedResponse)
    assert result.message is cached
    assert not embedder.calls
    assert not llm_client.calls
    session.commit.assert_not_awaited()
    assert session.execute.await_count == 3


# ---------------------------------------------------------------------------
# 5.2 — stale cache regenerates on tuple mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cache_triggers_regeneration() -> None:
    scope = _scope()
    ticket = _ticket(scope)
    stale = _suggestion_message(
        ticket_id=ticket.id, created_at=datetime.now(UTC) - timedelta(hours=1)
    )
    chunk = _knowledge_chunk(organization_id=scope.organization_id)

    session = _session_returning(
        _execute_result(scalar=ticket),  # unlocked get_or_404
        _execute_result(scalar=stale),  # _latest_suggestion (unlocked)
        _execute_result(scalar=True),  # _is_fresh: superseded -> stale
        _execute_result(scalar=ticket),  # locked get_or_404
        _execute_result(scalar=stale),  # _latest_suggestion (re-check)
        _execute_result(scalar=True),  # _is_fresh (re-check): still stale
        _execute_result(scalars_list=[]),  # thread messages
        _rows_result([(chunk, 0.9)]),  # chunk search
        _rows_result([]),  # ticket search
        MagicMock(),  # session.add is not execute; placeholder unused
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient()

    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    assert isinstance(result, SuggestedResponse)
    assert result.message is not stale
    assert result.message.content == "Try replacing the washer."
    assert result.message.is_ai_suggestion is True
    assert result.message.author_type == AuthorType.BOT
    assert embedder.calls  # regeneration actually ran retrieval
    assert llm_client.calls
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5.3 — empty retrieval returns None, LLM never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_retrieval_returns_none_and_never_calls_llm() -> None:
    scope = _scope()
    ticket = _ticket(scope)

    session = _session_returning(
        _execute_result(scalar=ticket),  # unlocked get_or_404
        _execute_result(scalar=None),  # _latest_suggestion (unlocked) -> no cache
        _execute_result(scalar=ticket),  # locked get_or_404
        _execute_result(scalar=None),  # _latest_suggestion (re-check) -> still none
        _execute_result(scalars_list=[]),  # thread messages
        _rows_result([]),  # chunk search - empty
        _rows_result([]),  # ticket search - empty
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient()

    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    assert result is None
    assert embedder.calls  # embedding still ran (needed to query)
    assert not llm_client.calls  # LLM must never be called for empty retrieval
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5.4 — org isolation: retrieval queries are scoped to the caller's org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieval_queries_are_scoped_to_the_callers_organization() -> None:
    scope = _scope()
    other_org_id = uuid4()
    ticket = _ticket(scope)
    own_chunk = _knowledge_chunk(organization_id=scope.organization_id)

    session = _session_returning(
        _execute_result(scalar=ticket),  # unlocked get_or_404
        _execute_result(scalar=None),  # _latest_suggestion (unlocked)
        _execute_result(scalar=ticket),  # locked get_or_404
        _execute_result(scalar=None),  # _latest_suggestion (re-check)
        _execute_result(scalars_list=[]),  # thread messages
        _rows_result([(own_chunk, 0.9)]),  # chunk search
        _rows_result([]),  # ticket search
        MagicMock(),
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient()

    await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    # The 6th and 7th `execute()` calls are the chunk/ticket-embedding
    # `search()` statements — compile them and assert they are scoped to
    # `scope.organization_id`, never to a different org's id.
    chunk_search_stmt = session.execute.call_args_list[5].args[0]
    ticket_search_stmt = session.execute.call_args_list[6].args[0]

    chunk_sql = str(chunk_search_stmt.compile(compile_kwargs={"literal_binds": True}))
    ticket_sql = str(ticket_search_stmt.compile(compile_kwargs={"literal_binds": True}))

    assert scope.organization_id.hex in chunk_sql
    assert other_org_id.hex not in chunk_sql
    assert scope.organization_id.hex in ticket_sql
    assert other_org_id.hex not in ticket_sql


# ---------------------------------------------------------------------------
# 5.5 — concurrent GET under get_or_404(for_update=True) yields exactly
# one inserted suggestion: the "loser" of the race finds the "winner"'s
# freshly-committed suggestion under the lock and returns it instead of
# generating a second one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_request_finds_winners_suggestion_under_lock_and_skips_generation() -> None:
    scope = _scope()
    ticket = _ticket(scope)
    winners_suggestion = _suggestion_message(ticket_id=ticket.id)

    session = _session_returning(
        _execute_result(scalar=ticket),  # unlocked get_or_404
        _execute_result(scalar=None),  # _latest_suggestion (unlocked) -> not yet visible
        _execute_result(scalar=ticket),  # locked get_or_404 -- lock acquired after winner committed
        _execute_result(scalar=winners_suggestion),  # _latest_suggestion (re-check) -> winner's row
        _execute_result(scalar=False),  # _is_fresh -> fresh (no message postdates it)
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient()

    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    assert isinstance(result, SuggestedResponse)
    assert result.message is winners_suggestion
    assert not embedder.calls  # the loser never regenerated
    assert not llm_client.calls
    session.commit.assert_not_awaited()  # the loser never wrote a second suggestion
    assert session.execute.await_count == 5


# ---------------------------------------------------------------------------
# 5.6 — GREEN: full happy path shape assertions (message persistence,
# top_similarity surfaced, prompt cites both source kinds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grounded_generation_persists_bot_message_and_surfaces_top_similarity() -> None:
    scope = _scope()
    ticket = _ticket(scope)
    chunk = _knowledge_chunk(organization_id=scope.organization_id, content="Deposit policy text.")
    ticket_embedding = _ticket_embedding(
        organization_id=scope.organization_id, summary="Similar leak fixed by replacing washer."
    )

    session = _session_returning(
        _execute_result(scalar=ticket),
        _execute_result(scalar=None),
        _execute_result(scalar=ticket),
        _execute_result(scalar=None),
        _execute_result(scalars_list=[_message(ticket_id=ticket.id, content="My faucet leaks.")]),
        _rows_result([(chunk, 0.81)]),
        _rows_result([(ticket_embedding, 0.65)]),
        MagicMock(),
    )
    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient(content="Per Knowledge Base Article 1, replace the washer.")

    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket.id, embedder=embedder, llm_client=llm_client
    )

    assert isinstance(result, SuggestedResponse)
    assert result.top_similarity == pytest.approx(0.81)
    assert result.message.content == "Per Knowledge Base Article 1, replace the washer."
    assert result.message.author_type == AuthorType.BOT
    assert result.message.is_ai_suggestion is True
    assert result.message.ticket_id == ticket.id
    session.add.assert_called_once()
    session.commit.assert_awaited_once()

    prompt = llm_client.calls[0]["messages"][0]["content"]
    assert "Knowledge Base Article 1" in prompt
    assert "Deposit policy text." in prompt
    assert "Past Ticket 1" in prompt
    assert "Similar leak fixed by replacing washer." in prompt
