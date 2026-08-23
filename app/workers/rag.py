"""Async arq jobs for Stage-3 RAG ingestion.

`embed_knowledge_document` is the per-article job the API layer enqueues
right after a knowledge base article is created or updated (`status`
reset to `"pending"` — see `app.api.v1.knowledge_base` and
`app.services.knowledge_base_service`). It chunks the article's content
(`app.services.chunking.chunk_document`), embeds every chunk via the
injected RAG embedding provider (`ctx["rag_embedder"]`,
`app.services.rag_embeddings.RagEmbeddingProvider`), and writes the
resulting `knowledge_chunks` rows.

`embed_resolved_ticket` is the per-ticket job the API layer enqueues right
after a ticket's status transitions into `RESOLVED`/`CLOSED` (see
`app.api.v1.tickets.update_ticket` and
`app.services.ticket_service.update_ticket`'s `entered_resolved_or_closed`
flag). It loads the ticket and its messages, asks an LLM
(`ctx.get("llm_client")`, defaulting to a real `openai.AsyncOpenAI` built
from `settings.openai_api_key` — same injectable-client pattern as
`app.services.llm_fallback.classify_with_llm`) for a resolution summary,
embeds that summary via `ctx["rag_embedder"]`, and UPSERTs it into
`ticket_embeddings` (`TicketEmbeddingRepository.upsert`, `ticket_id`
unique index) — so a resolved->closed double-fire or a
reopen->re-resolve regenerate both update the same row in place rather
than duplicating or erroring.

Both jobs open their own `AsyncSession` via `ctx["session_factory"]` —
mirrors `app.workers.classification.classify_ticket`'s job pattern
exactly. `embed_knowledge_document` additionally re-checks
`status == "pending"` under `get_or_404(..., for_update=True)` before
doing any work, so a duplicate delivery of the same job (arq's
at-least-once semantics) is a silent no-op; `embed_resolved_ticket` has
no such re-check to make since its target write is itself idempotent
(the `ticket_id`-unique upsert).

Error handling (shared `_is_transient_error` helper — do not duplicate
it):

- A transient failure (network error, rate limit, 5xx from the embedding
  provider or the LLM — see `_is_transient_error`) raises `arq.Retry`
  with an exponential backoff `defer`, letting arq's own retry policy
  (`WorkerSettings.max_tries`) retry the job later.
- A permanent failure (empty chunk set, wrong embedding dimension, an
  empty/unusable LLM summary, or any other non-transient exception):
  - `embed_knowledge_document` sets `status="failed"` and
    `embedding_error` on the article, commits, and returns normally.
  - `embed_resolved_ticket` logs a warning and returns normally, WITHOUT
    writing to `ticket_embeddings` — unlike `KnowledgeBase`, the
    `TicketEmbedding` model has no `status`/`embedding_error` column to
    record the failure on (see `app.models.ticket_embedding`), so
    structured logging is the only failure signal for this job. Arq must
    not retry a permanent failure either way.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import openai
from arq import Retry, create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.message import Message
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.repositories.ticket_embedding_repository import TicketEmbeddingRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.chunking import chunk_document
from app.services.llm_client import build_llm_client
from app.services.rag_embeddings import OPENAI_RAG_EMBEDDING_MODEL

logger = logging.getLogger(__name__)

# Exception types treated as transient (network/timeout-shaped) regardless
# of their source (httpx, openai, aiohttp, stdlib sockets, ...).
# `openai.APIConnectionError` covers its subclass `openai.APITimeoutError`
# too — neither is a stdlib `ConnectionError`/`TimeoutError`, so they need
# to be listed explicitly for `embed_resolved_ticket`'s LLM call to be
# retried on a network hiccup rather than treated as permanent.
_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    openai.APIConnectionError,
)

# Q3 business decision: the resolution summary fed into `ticket_embeddings`
# (used for RAG retrieval by other agents/tenants) must never carry the
# reporting tenant/owner/agent's identifiable personal details.
_RESOLUTION_SUMMARY_PROMPT_PREFIX = (
    "Summarize how the following support ticket was resolved, in 2-4 "
    "sentences, for future similar-ticket retrieval by support staff. Do "
    "NOT include any names, addresses, phone numbers, email addresses, or "
    "other personally identifiable, personal details of the tenant, "
    "owner, or agent involved — describe only the issue and its "
    "resolution in general terms.\n\n"
)


class PermanentEmbeddingError(Exception):
    """Raised for a non-retryable embedding failure (e.g. the document
    produced no chunks). Caught the same as any other permanent failure —
    see `embed_knowledge_document`."""


class PermanentSummaryError(Exception):
    """Raised for a non-retryable resolution-summarization failure (e.g.
    no OpenAI API key configured, or the LLM returned an empty summary).
    Caught the same as any other permanent failure — see
    `embed_resolved_ticket`."""


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


def _build_resolution_prompt(ticket: Any, messages: list[Message]) -> str:
    conversation = "\n".join(
        f"{message.author_type.value}: {message.content}" for message in messages
    )
    return (
        _RESOLUTION_SUMMARY_PROMPT_PREFIX
        + f"Ticket title: {ticket.title}\n"
        + f"Ticket description: {ticket.description or ''}\n\n"
        + f"Conversation:\n{conversation}\n"
    )


async def _summarize_resolution(*, ticket: Any, messages: list[Message], client: Any) -> str:
    """Ask the LLM for a PII-free resolution summary of `ticket`.

    `client` must be (or behave like) an `openai.AsyncOpenAI` instance; it
    is injected via `ctx.get("llm_client")` so tests never make a real
    network call — same pattern as `classify_with_llm`'s `client`
    parameter. When omitted, a real client is constructed from
    `settings.openai_api_key`.

    Raises `PermanentSummaryError` (never retried) when no API key is
    configured or the LLM returns an empty/blank summary; any
    `openai.OpenAIError` it raises otherwise propagates to the caller,
    which classifies it via `_is_transient_error`.
    """
    settings = get_settings()

    if client is None:
        if not settings.openai_api_key:
            raise PermanentSummaryError(
                "no OpenAI API key configured for resolution summarization"
            )
        client = build_llm_client(feature="rag_summary", api_key=settings.openai_api_key)

    completion = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": _build_resolution_prompt(ticket, messages)}],
    )
    content = completion.choices[0].message.content
    if content is None or not content.strip():
        raise PermanentSummaryError(
            f"LLM returned an empty resolution summary for ticket {ticket.id}"
        )
    return content.strip()


async def embed_resolved_ticket(
    ctx: dict[str, Any], ticket_id: UUID, organization_id: UUID
) -> None:
    """Summarize and embed a resolved/closed ticket, UPSERTing the result
    into `ticket_embeddings` (`ticket_id` unique index).

    See module docstring for the transient/permanent error split. Unlike
    `embed_knowledge_document`, there is no `status` column to re-check
    for idempotency: a duplicate delivery of this same job (or a
    resolved->closed double-fire, both re-enqueuing this job for the same
    `ticket_id`) simply re-runs the summarize/embed/upsert pipeline and
    updates the same row in place.
    """
    session_factory = ctx["session_factory"]
    rag_embedder = ctx["rag_embedder"]
    llm_client = ctx.get("llm_client")

    scope = OrgScope.for_background_worker(organization_id)

    async with session_factory() as session:
        ticket = await TicketRepository(session, scope).get_or_404(ticket_id)

        messages_stmt = (
            select(Message)
            .where(Message.ticket_id == ticket_id)
            .order_by(Message.created_at, Message.id)
        )
        messages = list((await session.execute(messages_stmt)).scalars().all())

        try:
            summary = await _summarize_resolution(
                ticket=ticket, messages=messages, client=llm_client
            )
            embeddings = await rag_embedder.embed([summary])
        except Exception as exc:
            if _is_transient_error(exc):
                job_try = ctx.get("job_try", 1)
                raise Retry(defer=2**job_try) from exc

            logger.warning(
                "permanent embedding failure for resolved ticket %s; "
                "ticket_embeddings has no status/embedding_error column to "
                "record this on, so this log line is the only failure signal",
                ticket_id,
                exc_info=True,
            )
            return

        await TicketEmbeddingRepository(session, scope).upsert(
            ticket_id,
            organization_id,
            summary,
            embeddings[0],
            model_used=OPENAI_RAG_EMBEDDING_MODEL,
        )
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


async def enqueue_resolved_ticket_embedding(ticket_id: UUID, organization_id: UUID) -> None:
    """Enqueue an `embed_resolved_ticket` job for a ticket whose status
    just transitioned into `RESOLVED`/`CLOSED`.

    Called from the API layer via `fastapi.BackgroundTasks`, right after
    the updating request commits — see `app.api.v1.tickets.update_ticket`.
    Mirrors `enqueue_kb_embedding`'s short-lived per-call Redis pool
    exactly: always closed, even if the enqueue itself raises.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("embed_resolved_ticket", ticket_id, organization_id)
    finally:
        await redis.aclose()
