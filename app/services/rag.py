"""RAG suggestion pipeline (Work Unit 5, PR6): `suggest_response`
synchronously retrieves grounded context (knowledge base chunks + past
resolved-ticket summaries) for a ticket's current conversation thread and
asks an LLM to draft a reply citing its sources, persisting the result as
a `messages` row (`author_type=AuthorType.BOT`, `is_ai_suggestion=True`).

This is a plain request-path SERVICE function (same house style as
`app.services.ticket_service`/`app.services.message_service` — a `session`
+ `scope` kwarg-only signature), NOT a worker job — there is no job queue
in this path. Any LLM/embedding failure during the live GET request
(PR7's `GET /tickets/:id/suggested-response`) simply propagates as an
exception, mapped to a 5xx by the API layer, rather than an `arq.Retry`:
retries only make sense for a queued background job that can be
redelivered later (see `app.workers.rag`'s `_is_transient_error`); a
synchronous request has no such mechanism, and the caller is expected to
just retry the request itself if it wants another attempt. There is
therefore no `_is_transient_error`-style split in this module.

Caching (design's resolved Q2/Q5): the most recent `is_ai_suggestion=True`
message for a ticket is reused as-is as long as it is still "fresh" — no
non-AI message has been posted after it (`_latest_suggestion`/`_is_fresh`).
A concurrent double-GET on the same ticket is resolved with the exact same
`get_or_404(id, for_update=True)` row-lock pattern
`app.workers.classification.classify_ticket` and `app.workers.rag`'s jobs
use: an unlocked fast path first (the common case — most GETs hit an
already-fresh cache and never need the lock at all), then, only on a
cache miss/staleness, a locked re-check before doing any LLM/embedding
work, so two concurrent requests for the same ticket never both regenerate
and insert two suggestion rows — the loser's locked re-check simply finds
the winner's freshly-inserted row and returns it instead of generating a
second one.

Empty retrieval (design's resolved Q4): if BOTH the knowledge-chunk and
past-ticket-summary searches return zero rows above
`settings.rag_similarity_threshold`, this returns `None` WITHOUT ever
calling the LLM — the caller (PR7's API route) turns that into a "no
grounded suggestion available" response.

Tenant isolation: both retrieval queries go through
`KnowledgeChunkRepository.search`/`TicketEmbeddingRepository.search`,
which apply `scope_clause()` (see `app.repositories.base.ScopedRepository`)
— org B's `suggest_response` call structurally cannot retrieve org A's
chunks/summaries, same guarantee PR2's repository tests already proved at
the compiled-SQL level for `search()` itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.enums import AuthorType
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.message import Message
from app.models.ticket_embedding import TicketEmbedding
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.ticket_embedding_repository import TicketEmbeddingRepository
from app.repositories.ticket_repository import TicketRepository

# Q3-style business decision carried over from `app.workers.rag`'s
# resolution-summary prompt: the draft must ground itself ONLY in the
# retrieved sources and must cite which one(s) it used, so the caller can
# show the agent where the suggestion came from.
_SUGGESTION_PROMPT_PREFIX = (
    "Draft a helpful, professional reply to the following support ticket "
    "thread. Ground your reply ONLY in the knowledge base excerpts and "
    "past resolved-ticket summaries provided below, and explicitly cite "
    "which source(s) you drew from (e.g. \"per Knowledge Base Article 2\" "
    "or \"per Past Ticket 1\"). Do not invent information that is not "
    "present in the provided sources.\n\n"
)


@dataclass(frozen=True)
class SuggestedResponse:
    """A grounded AI suggestion returned by `suggest_response`.

    `top_similarity` is the highest cosine similarity score among the
    retrieved chunks/summaries that grounded a NEWLY GENERATED suggestion.
    It is `None` for a cache hit (`suggest_response` returns the existing
    `messages` row as-is without re-running retrieval) — the caller
    already got that score when the suggestion was first generated.
    """

    message: Message
    top_similarity: float | None


async def _latest_suggestion(session: AsyncSession, ticket_id: UUID) -> Message | None:
    """The most recent AI-suggestion message for `ticket_id`, if any."""
    return (
        await session.execute(
            select(Message)
            .where(Message.ticket_id == ticket_id, Message.is_ai_suggestion.is_(True))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _is_fresh(session: AsyncSession, suggestion: Message) -> bool:
    """Stale iff any NON-AI message sorts strictly after `suggestion` by
    `(created_at, id)` — i.e. the human/agent conversation moved on since
    this suggestion was generated."""
    superseded = (
        select(Message.id)
        .where(
            Message.ticket_id == suggestion.ticket_id,
            Message.is_ai_suggestion.is_(False),
            tuple_(Message.created_at, Message.id)
            > tuple_(suggestion.created_at, suggestion.id),
        )
        .limit(1)
    )
    return (await session.execute(select(superseded.exists()))).scalar_one() is False


def _build_thread_text(ticket: Any, messages: list[Message]) -> str:
    """Assemble the ticket's current conversation thread (title,
    description, and every non-AI-suggestion message so far, oldest
    first) as a single string to embed for retrieval — analogous to
    `app.workers.rag._build_resolution_prompt`'s conversation
    concatenation, built from the ticket's title/description plus the
    reporting/agent messages seen so far rather than a full resolved
    conversation."""
    conversation = "\n".join(
        f"{message.author_type.value}: {message.content}" for message in messages
    )
    return (
        f"Ticket title: {ticket.title}\n"
        + f"Ticket description: {ticket.description or ''}\n\n"
        + f"Conversation:\n{conversation}\n"
    )


def _build_suggestion_prompt(
    thread_text: str,
    chunk_rows: Sequence[Any],
    ticket_rows: Sequence[Any],
) -> str:
    """Build the LLM prompt from the current thread plus both retrieved
    source sets. `chunk_rows`/`ticket_rows` are `(model_instance,
    similarity)` row pairs, in relevance order (best match first) — same
    shape `KnowledgeChunkRepository.search`/`TicketEmbeddingRepository
    .search` return."""
    kb_section = "\n".join(
        f"Knowledge Base Article {index + 1}: {chunk.content}"
        for index, (chunk, _similarity) in enumerate(chunk_rows)
    )
    past_ticket_section = "\n".join(
        f"Past Ticket {index + 1}: {ticket_embedding.summary}"
        for index, (ticket_embedding, _similarity) in enumerate(ticket_rows)
    )

    prompt = _SUGGESTION_PROMPT_PREFIX + thread_text
    if kb_section:
        prompt += f"\nKnowledge base excerpts:\n{kb_section}\n"
    if past_ticket_section:
        prompt += f"\nPast resolved ticket summaries:\n{past_ticket_section}\n"
    return prompt


async def suggest_response(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket_id: UUID,
    embedder: Any,
    llm_client: Any,
) -> SuggestedResponse | None:
    """Return a grounded AI-drafted reply suggestion for `ticket_id`,
    reusing a still-fresh cached suggestion when one exists, or `None`
    when retrieval finds no grounded source at all (never falls back to
    an ungrounded LLM guess — see module docstring's Q4 note).

    Raises `app.core.exceptions.NotFoundError` under the same visibility
    rule `TicketRepository.get_or_404` already enforces for every other
    ticket-scoped service function (wrong org, or a same-org-but-role
    -invisible ticket, 404s identically).
    """
    settings = get_settings()

    # Unlocked fast path: the common case is an already-fresh cached
    # suggestion, which never needs the row lock at all.
    await TicketRepository(session, scope).get_or_404(ticket_id)

    cached = await _latest_suggestion(session, ticket_id)
    if cached is not None and await _is_fresh(session, cached):
        return SuggestedResponse(message=cached, top_similarity=None)

    # Cache miss or stale: acquire the row lock before doing any
    # LLM/embedding work, then re-check under the lock — a concurrent
    # request may have already regenerated and committed a fresh
    # suggestion while we were computing the above.
    ticket = await TicketRepository(session, scope).get_or_404(ticket_id, for_update=True)

    cached = await _latest_suggestion(session, ticket_id)
    if cached is not None and await _is_fresh(session, cached):
        return SuggestedResponse(message=cached, top_similarity=None)  # a concurrent request won

    thread_messages_stmt = (
        select(Message)
        .where(Message.ticket_id == ticket_id, Message.is_ai_suggestion.is_(False))
        .order_by(Message.created_at, Message.id)
    )
    thread_messages = list((await session.execute(thread_messages_stmt)).scalars().all())
    thread_text = _build_thread_text(ticket, thread_messages)

    embeddings = await embedder.embed([thread_text])
    query_embedding = embeddings[0]

    chunk_rows: list[tuple[KnowledgeChunk, float]] = (
        await session.execute(
            KnowledgeChunkRepository(session, scope).search(
                query_embedding,
                limit=settings.rag_kb_top_k,
                min_similarity=settings.rag_similarity_threshold,
            )
        )
    ).all()[: settings.rag_kb_top_k]

    ticket_rows: list[tuple[TicketEmbedding, float]] = (
        await session.execute(
            TicketEmbeddingRepository(session, scope).search(
                query_embedding,
                limit=settings.rag_ticket_top_k,
                min_similarity=settings.rag_similarity_threshold,
            )
        )
    ).all()[: settings.rag_ticket_top_k]

    if not chunk_rows and not ticket_rows:
        # Q4: no grounded source at all — never call the LLM, never
        # persist an ungrounded guess. The caller (PR7) turns this `None`
        # into the "no grounded suggestion available" response.
        return None

    top_similarity = max(
        [float(similarity) for _chunk, similarity in chunk_rows]
        + [float(similarity) for _ticket_embedding, similarity in ticket_rows]
    )

    prompt = _build_suggestion_prompt(thread_text, chunk_rows, ticket_rows)
    completion = await llm_client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": prompt}],
    )
    draft = completion.choices[0].message.content
    if draft is None or not draft.strip():
        raise ValueError(f"LLM returned an empty suggestion draft for ticket {ticket_id}")

    repo = MessageRepository(session, scope)
    suggestion = repo.add(
        ticket_id=ticket_id,
        author_type=AuthorType.BOT,
        content=draft.strip(),
        is_ai_suggestion=True,
    )
    # Commit (not just flush): this releases the row lock acquired above,
    # same as `app.workers.classification.classify_ticket`'s and
    # `app.workers.rag.embed_resolved_ticket`'s own locked critical
    # sections.
    await session.commit()

    return SuggestedResponse(message=suggestion, top_similarity=top_similarity)
