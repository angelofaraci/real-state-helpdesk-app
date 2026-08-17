"""Knowledge base CRUD service (org-scoped).

Authorization is enforced at the API layer
(`app.api.deps.require_org_admin` for writes, `require_org_staff` for
reads), not here — this module only handles persistence and the
pending/re-embed lifecycle.

`create_knowledge_base`/`update_knowledge_base` always leave the row at
`status="pending"` (clearing `embedding_error`); the API layer is
responsible for enqueuing `app.workers.rag.embed_knowledge_document`
right after (via `app.workers.rag.enqueue_kb_embedding`), so the worker
picks up ingestion asynchronously.

`delete_knowledge_base` hard-deletes the row AND explicitly deletes its
`knowledge_chunks` first, via `KnowledgeChunkRepository
.delete_for_knowledge_base`. The `knowledge_chunks.knowledge_base_id` FK
already carries `ondelete="CASCADE"` (migration 0004), but that DB-level
cascade is never exercised by this app's unit test suite (mocked
sessions, no live Postgres) — the explicit app-level delete here is what
`tests/unit/test_knowledge_base_service.py` actually proves.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.knowledge_base import KnowledgeBase
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository


async def create_knowledge_base(
    session: AsyncSession,
    *,
    scope: OrgScope,
    title: str,
    content: str,
    source_type: str | None = None,
) -> KnowledgeBase:
    """Create a knowledge base article in `scope`'s organization, always
    starting at `status="pending"`."""
    repo = KnowledgeBaseRepository(session, scope)
    knowledge_base = repo.add(
        title=title,
        content=content,
        source_type=source_type,
        status="pending",
        embedding_error=None,
    )
    await session.flush()
    return knowledge_base


async def list_knowledge_base(session: AsyncSession, *, scope: OrgScope) -> list[KnowledgeBase]:
    """List every knowledge base article in `scope`'s organization."""
    repo = KnowledgeBaseRepository(session, scope)
    result = await session.execute(repo.select())
    return list(result.scalars().all())


async def get_knowledge_base(
    session: AsyncSession, *, scope: OrgScope, knowledge_base_id: UUID
) -> KnowledgeBase:
    """Fetch a single knowledge base article in `scope`'s organization.
    Raises `app.core.exceptions.NotFoundError` if it does not exist or
    belongs to a different organization."""
    return await KnowledgeBaseRepository(session, scope).get_or_404(knowledge_base_id)


async def update_knowledge_base(
    session: AsyncSession, *, scope: OrgScope, knowledge_base_id: UUID, **fields: object
) -> KnowledgeBase:
    """Update `title`/`content`/`source_type` on a knowledge base article
    in `scope`'s organization. ANY supplied field resets `status` back to
    `"pending"` and clears `embedding_error` — the API layer re-enqueues
    `embed_knowledge_document` right after, so a stale "ready"/"failed"
    article is never left un-re-embedded after an edit."""
    repo = KnowledgeBaseRepository(session, scope)
    fields = dict(fields)
    fields["status"] = "pending"
    fields["embedding_error"] = None
    knowledge_base = await repo.update(knowledge_base_id, **fields)
    await session.flush()
    return knowledge_base


async def delete_knowledge_base(
    session: AsyncSession, *, scope: OrgScope, knowledge_base_id: UUID
) -> None:
    """Hard-delete a knowledge base article and all of its
    `knowledge_chunks` rows, explicitly (see module docstring)."""
    kb_repo = KnowledgeBaseRepository(session, scope)
    chunk_repo = KnowledgeChunkRepository(session, scope)

    knowledge_base = await kb_repo.get_or_404(knowledge_base_id)
    await chunk_repo.delete_for_knowledge_base(knowledge_base_id)
    await session.delete(knowledge_base)
    await session.flush()
