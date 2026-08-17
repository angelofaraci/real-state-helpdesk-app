"""Unit tests for `app.workers.rag` — the async RAG ingestion worker jobs
(`embed_knowledge_document`, `embed_resolved_ticket`) and their enqueue
helpers (`enqueue_kb_embedding`, `enqueue_resolved_ticket_embedding`).

`AsyncSession` is mocked, following the exact pattern established by
`tests/unit/test_worker_classification.py`: the worker opens/commits its
own session via an injected `ctx["session_factory"]`, so `session_factory`
here is a plain callable returning an async context manager that yields
the mocked session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import arq
import pytest
from sqlalchemy.dialects import postgresql

from app.models.enums import AuthorType, TicketChannel, TicketStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.ticket import Ticket
from app.workers import rag as worker


class _SessionContextManager:
    """Minimal async context manager wrapping an already-built mock
    session — stands in for calling a real `async_sessionmaker` instance."""

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


class _FakeEmbedder:
    def __init__(self, vectors=None, side_effect=None) -> None:
        self._vectors = vectors
        self._side_effect = side_effect

    async def embed(self, texts):
        if self._side_effect is not None:
            raise self._side_effect
        return self._vectors if self._vectors is not None else [[0.1] * 768 for _ in texts]


def _knowledge_base(*, organization_id, status="pending", **overrides) -> KnowledgeBase:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        title="Pet policy",
        content="Pets are allowed with a refundable deposit.\n\nCats and dogs only.",
        source_type=None,
        status=status,
        embedding_error=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return KnowledgeBase(**defaults)


# ---------------------------------------------------------------------------
# embed_knowledge_document — success path (pending -> ready)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_chunks_embeds_and_marks_ready() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id)

    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
    }

    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert kb.status == "ready"
    assert kb.embedding_error is None
    session.commit.assert_awaited_once()
    session.add.assert_called()  # KnowledgeChunkRepository.add() staged chunk rows


@pytest.mark.asyncio
async def test_get_or_404_is_called_with_for_update() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id, status="ready")
    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
    }

    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    (stmt,), _ = session.execute.call_args_list[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in sql


# ---------------------------------------------------------------------------
# embed_knowledge_document — idempotency (already-processed under the lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_pending_status_is_a_no_op_no_double_embed() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id, status="ready")
    session = _session_returning(_execute_result(scalar=kb))
    fake_embedder = _FakeEmbedder()
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": fake_embedder,
    }

    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    session.commit.assert_not_awaited()
    # Only the single `get_or_404` lookup happened.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_processing_status_is_also_a_no_op() -> None:
    """`status="processing"` (mid-flight from a concurrent delivery) must
    not be re-embedded either — only `"pending"` triggers work."""
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id, status="processing")
    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
    }

    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    session.commit.assert_not_awaited()
    assert kb.status == "processing"


# ---------------------------------------------------------------------------
# embed_knowledge_document — permanent failure (empty chunk set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_document_marks_failed_with_embedding_error_and_does_not_raise() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id, content="   \n\n   ")
    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
    }

    # Must not raise.
    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert kb.status == "failed"
    assert kb.embedding_error is not None
    assert "no chunks" in kb.embedding_error
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dimension_mismatch_marks_failed_and_does_not_raise() -> None:
    from app.services.rag_embeddings import RagEmbeddingDimensionError

    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id)
    session = _session_returning(_execute_result(scalar=kb))
    # A real provider raises `RagEmbeddingDimensionError` (see
    # `app.services.rag_embeddings._require_dim`) when its output doesn't
    # match `RAG_EMBEDDING_DIM` — a permanent, non-retryable failure.
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(
            side_effect=RagEmbeddingDimensionError("RAG embedding must be 768-dim, got 2")
        ),
    }

    await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert kb.status == "failed"
    assert kb.embedding_error is not None
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# embed_knowledge_document — transient failure (retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_error_raises_arq_retry_with_backoff() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id)
    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(side_effect=ConnectionError("network down")),
        "job_try": 2,
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert exc_info.value.defer_score == (2**2) * 1000
    assert kb.status == "pending"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limit_status_code_raises_arq_retry() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id)
    session = _session_returning(_execute_result(scalar=kb))

    class _RateLimitError(Exception):
        status_code = 429

    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(side_effect=_RateLimitError("rate limited")),
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert exc_info.value.defer_score == (2**1) * 1000
    assert kb.status == "pending"


@pytest.mark.asyncio
async def test_transient_failure_defaults_job_try_to_one_when_absent() -> None:
    organization_id = uuid4()
    kb = _knowledge_base(organization_id=organization_id)
    session = _session_returning(_execute_result(scalar=kb))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(side_effect=TimeoutError("timed out")),
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.embed_knowledge_document(ctx, kb.id, organization_id)

    assert exc_info.value.defer_score == (2**1) * 1000


# ---------------------------------------------------------------------------
# enqueue_kb_embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_kb_embedding_opens_a_pool_enqueues_and_closes(monkeypatch) -> None:
    knowledge_base_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_create_pool = AsyncMock(return_value=fake_redis)
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    await worker.enqueue_kb_embedding(knowledge_base_id, organization_id)

    fake_create_pool.assert_awaited_once()
    fake_redis.enqueue_job.assert_awaited_once_with(
        "embed_knowledge_document", knowledge_base_id, organization_id
    )
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_kb_embedding_closes_the_pool_even_if_enqueue_fails(monkeypatch) -> None:
    knowledge_base_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_redis.enqueue_job.side_effect = RuntimeError("redis unreachable")
    monkeypatch.setattr(worker, "create_pool", AsyncMock(return_value=fake_redis))

    with pytest.raises(RuntimeError):
        await worker.enqueue_kb_embedding(knowledge_base_id, organization_id)

    fake_redis.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# embed_resolved_ticket — helpers
#
# No live Postgres is available in this sandbox: idempotency is verified by
# asserting the job's DB write compiles to `INSERT ... ON CONFLICT
# (ticket_id) DO UPDATE` (the real `TicketEmbeddingRepository.upsert`, not
# a mock standing in for it) across repeated job runs for the same ticket,
# same approach `tests/unit/test_ticket_embedding_repository.py` uses for
# the repository directly.
# ---------------------------------------------------------------------------


def _ticket(*, organization_id, ticket_id=None, **overrides) -> Ticket:
    defaults = dict(
        id=ticket_id or uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        title="Leaking faucet",
        description="The kitchen faucet has been dripping for a week.",
        channel=TicketChannel.WEB,
        status=TicketStatus.RESOLVED,
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _message(*, ticket_id, content, author_type=AuthorType.USER, **overrides) -> Message:
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


def _scalar_result(scalar: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


def _scalars_result(items: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _session_with_execute_results(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


class _FakeLLMClient:
    """Stand-in for an `openai.AsyncOpenAI`-shaped client — only the
    `chat.completions.create(...)` surface `embed_resolved_ticket` uses."""

    def __init__(self, content: str | None = "A dripping kitchen faucet was repaired.",
                 side_effect: BaseException | None = None) -> None:
        self._content = content
        self._side_effect = side_effect
        self.calls: list[dict] = []

        class _Completions:
            def __init__(self, outer: "_FakeLLMClient") -> None:
                self._outer = outer

            async def create(self, **kwargs):
                self._outer.calls.append(kwargs)
                if self._outer._side_effect is not None:
                    raise self._outer._side_effect
                message = MagicMock(content=self._outer._content)
                choice = MagicMock(message=message)
                return MagicMock(choices=[choice])

        self.chat = MagicMock(completions=_Completions(self))


def _upsert_sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# embed_resolved_ticket — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_summarizes_embeds_and_upserts() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [
        _message(ticket_id=ticket.id, content="My faucet is leaking."),
        _message(
            ticket_id=ticket.id,
            content="An agent replaced the washer; issue resolved.",
            author_type=AuthorType.AGENT,
        ),
    ]
    session = _session_with_execute_results(
        _scalar_result(ticket), _scalars_result(messages), MagicMock()
    )
    llm_client = _FakeLLMClient()
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": llm_client,
    }

    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    session.commit.assert_awaited_once()
    (stmt,), _ = session.execute.call_args_list[2]
    sql = _upsert_sql(stmt)
    assert "INSERT INTO ticket_embeddings" in sql
    assert "ON CONFLICT (ticket_id) DO UPDATE" in sql
    assert str(ticket.id) in sql
    assert llm_client.calls  # the LLM was actually called to summarize


@pytest.mark.asyncio
async def test_resolution_prompt_excludes_pii_instruction_is_present() -> None:
    """Q3 business decision: the prompt itself must instruct the LLM to
    exclude names/addresses/identifiable personal details."""
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [_message(ticket_id=ticket.id, content="My faucet is leaking.")]
    session = _session_with_execute_results(
        _scalar_result(ticket), _scalars_result(messages), MagicMock()
    )
    llm_client = _FakeLLMClient()
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": llm_client,
    }

    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    prompt = llm_client.calls[0]["messages"][0]["content"]
    assert "name" in prompt.lower()
    assert "address" in prompt.lower()
    assert "personal" in prompt.lower() or "identifiable" in prompt.lower()


# ---------------------------------------------------------------------------
# embed_resolved_ticket — idempotency: resolved->closed double-fire and
# reopen->re-resolve both upsert (never insert-only / never raise / never
# duplicate).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_then_closed_double_fire_upserts_twice_same_ticket() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id, status=TicketStatus.RESOLVED)
    messages = [_message(ticket_id=ticket.id, content="Leak fixed.")]

    session = _session_with_execute_results(
        _scalar_result(ticket),
        _scalars_result(messages),
        MagicMock(),
        _scalar_result(ticket),
        _scalars_result(messages),
        MagicMock(),
    )
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": _FakeLLMClient(),
    }

    # First fire: transition into RESOLVED.
    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)
    # Second fire: transition RESOLVED -> CLOSED (double delivery scenario).
    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    assert session.execute.await_count == 6
    upsert_calls = [session.execute.call_args_list[2], session.execute.call_args_list[5]]
    for call in upsert_calls:
        (stmt,), _ = call
        sql = _upsert_sql(stmt)
        assert "INSERT INTO ticket_embeddings" in sql
        assert "ON CONFLICT (ticket_id) DO UPDATE" in sql
        assert str(ticket.id) in sql


@pytest.mark.asyncio
async def test_reopen_then_reresolve_regenerates_summary_via_upsert() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id, status=TicketStatus.RESOLVED)
    first_messages = [_message(ticket_id=ticket.id, content="Original issue resolved.")]
    second_messages = [
        _message(ticket_id=ticket.id, content="Original issue resolved."),
        _message(ticket_id=ticket.id, content="Issue reopened."),
        _message(ticket_id=ticket.id, content="Re-resolved after a second visit."),
    ]

    session = _session_with_execute_results(
        _scalar_result(ticket),
        _scalars_result(first_messages),
        MagicMock(),
        _scalar_result(ticket),
        _scalars_result(second_messages),
        MagicMock(),
    )
    llm_client = _FakeLLMClient(content="Second, distinct resolution summary.")
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": llm_client,
    }

    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)
    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    first_sql = _upsert_sql(session.execute.call_args_list[2].args[0])
    second_sql = _upsert_sql(session.execute.call_args_list[5].args[0])
    assert "ON CONFLICT (ticket_id) DO UPDATE" in first_sql
    assert "ON CONFLICT (ticket_id) DO UPDATE" in second_sql
    assert "'Second, distinct resolution summary.'" in second_sql


# ---------------------------------------------------------------------------
# embed_resolved_ticket — transient failure (retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_connection_error_raises_arq_retry_with_backoff() -> None:
    import openai

    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [_message(ticket_id=ticket.id, content="Leak fixed.")]
    session = _session_with_execute_results(_scalar_result(ticket), _scalars_result(messages))
    llm_client = _FakeLLMClient(
        side_effect=openai.APIConnectionError(request=MagicMock())
    )
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": llm_client,
        "job_try": 2,
    }

    with pytest.raises(arq.Retry) as exc_info:
        await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    assert exc_info.value.defer_score == (2**2) * 1000
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedder_connection_error_raises_arq_retry() -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [_message(ticket_id=ticket.id, content="Leak fixed.")]
    session = _session_with_execute_results(_scalar_result(ticket), _scalars_result(messages))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(side_effect=ConnectionError("network down")),
        "llm_client": _FakeLLMClient(),
    }

    with pytest.raises(arq.Retry):
        await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# embed_resolved_ticket — permanent failure: logged, no write, no raise
# (ticket_embeddings has no status/embedding_error column to record it on).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_llm_summary_is_a_permanent_failure_logged_no_raise(caplog) -> None:
    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [_message(ticket_id=ticket.id, content="Leak fixed.")]
    session = _session_with_execute_results(_scalar_result(ticket), _scalars_result(messages))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(),
        "llm_client": _FakeLLMClient(content="   "),
    }

    # Must not raise.
    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    session.commit.assert_not_awaited()
    # Only the ticket + messages lookups happened — no write attempted.
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_dimension_mismatch_is_a_permanent_failure_logged_no_raise() -> None:
    from app.services.rag_embeddings import RagEmbeddingDimensionError

    organization_id = uuid4()
    ticket = _ticket(organization_id=organization_id)
    messages = [_message(ticket_id=ticket.id, content="Leak fixed.")]
    session = _session_with_execute_results(_scalar_result(ticket), _scalars_result(messages))
    ctx = {
        "session_factory": _session_factory(session),
        "rag_embedder": _FakeEmbedder(
            side_effect=RagEmbeddingDimensionError("RAG embedding must be 768-dim, got 2")
        ),
        "llm_client": _FakeLLMClient(),
    }

    await worker.embed_resolved_ticket(ctx, ticket.id, organization_id)

    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# enqueue_resolved_ticket_embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_resolved_ticket_embedding_opens_a_pool_enqueues_and_closes(
    monkeypatch,
) -> None:
    ticket_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_create_pool = AsyncMock(return_value=fake_redis)
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    await worker.enqueue_resolved_ticket_embedding(ticket_id, organization_id)

    fake_create_pool.assert_awaited_once()
    fake_redis.enqueue_job.assert_awaited_once_with(
        "embed_resolved_ticket", ticket_id, organization_id
    )
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_resolved_ticket_embedding_closes_the_pool_even_if_enqueue_fails(
    monkeypatch,
) -> None:
    ticket_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_redis.enqueue_job.side_effect = RuntimeError("redis unreachable")
    monkeypatch.setattr(worker, "create_pool", AsyncMock(return_value=fake_redis))

    with pytest.raises(RuntimeError):
        await worker.enqueue_resolved_ticket_embedding(ticket_id, organization_id)

    fake_redis.aclose.assert_awaited_once()
