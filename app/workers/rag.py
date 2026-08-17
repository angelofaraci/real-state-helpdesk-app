"""Async arq job for Stage-3 RAG knowledge-base ingestion.

`embed_knowledge_document` is the per-article job the API layer enqueues
right after a knowledge base article is created or updated (`status`
reset to `"pending"` — see `app.api.v1.knowledge_base` and
`app.services.knowledge_base_service`). It chunks the article's content
(`app.services.chunking.chunk_document`), embeds every chunk via the
injected RAG embedding provider (`ctx["rag_embedder"]`,
`app.services.rag_embeddings.RagEmbeddingProvider`), and writes the
resulting `knowledge_chunks` rows.

Opens its own `AsyncSession` via `ctx["session_factory"]` — mirrors
`app.workers.classification.classify_ticket`'s job pattern exactly,
including re-checking `status == "pending"` under `get_or_404(...,
for_update=True)` before doing any work, so a duplicate delivery of the
same job (arq's at-least-once semantics) is a silent no-op.

Error handling:

- A transient failure (network error, rate limit, 5xx from the embedding
  provider — see `_is_transient_error`) raises `arq.Retry` with an
  exponential backoff `defer`, letting arq's own retry policy
  (`WorkerSettings.max_tries`) retry the job later.
- A permanent failure (empty chunk set, wrong embedding dimension, or any
  other non-transient exception) sets `status="failed"` and
  `embedding_error` on the article, commits, and returns normally — arq
  must not retry a permanent failure.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from arq import Retry, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.services.chunking import chunk_document

logger = logging.getLogger(__name__)

# Exception types treated as transient (network/timeout-shaped) regardless
# of their source (httpx, openai, aiohttp, stdlib sockets, ...).
_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
)


class PermanentEmbeddingError(Exception):
    """Raised for a non-retryable embedding failure (e.g. the document
    produced no chunks). Caught the same as any other permanent failure —
    see `embed_knowledge_document`."""


def _is_transient_error(exc: BaseException) -> bool:
    """True for a network-shaped/rate-limit/5xx failure that should be
    retried; False for anything that should permanently fail the job
    (bad input, dimension mismatch, auth errors, etc.).

    Checks both the exception's type (stdlib `ConnectionError`/
    `TimeoutError`, which most HTTP client libraries raise or wrap on
    network failure) and a `status_code`/`http_status` attribute some SDK
    exceptions (e.g. `openai.APIStatusError`) carry, treating 429 (rate
    limit) and any 5xx as transient.
    """
    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return True

    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True

    return False


async def embed_knowledge_document(
    ctx: dict[str, Any], knowledge_base_id: UUID, organization_id: UUID
) -> None:
    """Chunk and embed a knowledge base article, persisting the resulting
    `knowledge_chunks` rows and transitioning `status` `"pending"` ->
    `"ready"` (or `"failed"` on a permanent error).

    See module docstring for the transient/permanent error split and the
    idempotency guard.
    """
    session_factory = ctx["session_factory"]
    rag_embedder = ctx["rag_embedder"]

    scope = OrgScope.for_background_worker(organization_id)

    async with session_factory() as session:
        kb_repo = KnowledgeBaseRepository(session, scope)
        chunk_repo = KnowledgeChunkRepository(session, scope)

        knowledge_base = await kb_repo.get_or_404(knowledge_base_id, for_update=True)

        if knowledge_base.status != "pending":
            # Already processed (or otherwise no longer pending) by the
            # time we acquired the lock — a duplicate delivery of this
            # same job. Nothing to do.
            return

        try:
            chunks = chunk_document(knowledge_base.content)
            if not chunks:
                raise PermanentEmbeddingError(
                    f"knowledge base {knowledge_base_id} produced no chunks"
                )

            embeddings = await rag_embedder.embed([chunk.content for chunk in chunks])
        except Exception as exc:
            if _is_transient_error(exc):
                job_try = ctx.get("job_try", 1)
                raise Retry(defer=2**job_try) from exc

            logger.warning(
                "permanent embedding failure for knowledge base %s",
                knowledge_base_id,
                exc_info=True,
            )
            knowledge_base.status = "failed"
            knowledge_base.embedding_error = str(exc)[:1000]
            await session.commit()
            return

        for chunk, embedding in zip(chunks, embeddings, strict=True):
            chunk_repo.add(
                knowledge_base_id=knowledge_base_id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=embedding,
            )

        knowledge_base.status = "ready"
        await session.commit()


async def enqueue_kb_embedding(knowledge_base_id: UUID, organization_id: UUID) -> None:
    """Enqueue an `embed_knowledge_document` job for a just-created or
    just-updated knowledge base article.

    Called from the API layer via `fastapi.BackgroundTasks`, right after
    the creating/updating request commits — see
    `app.api.v1.knowledge_base`. Mirrors
    `app.workers.classification.enqueue_classification`'s short-lived
    per-call Redis pool exactly: always closed, even if the enqueue
    itself raises.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("embed_knowledge_document", knowledge_base_id, organization_id)
    finally:
        await redis.aclose()
