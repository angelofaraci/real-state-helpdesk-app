"""Unit tests for `app.workers.rag` — the async RAG ingestion worker job
(`embed_knowledge_document`) and its enqueue helper
(`enqueue_kb_embedding`).

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

from app.models.knowledge_base import KnowledgeBase
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
