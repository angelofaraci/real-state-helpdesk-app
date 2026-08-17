"""Org-scoped knowledge-base CRUD endpoints + RAG ingestion trigger
(Stage 3, PR4).

Write access (`POST`/`PATCH`/`DELETE`) is admin-only via
`require_org_admin`. Read access (`GET`, list + by id) is available to
admins and agents via `require_org_staff` — the same precedent
`app.api.v1.categories` established for reference/taxonomy-adjacent data;
a tenant/owner has no business reading the org's knowledge base directly.

`POST`/`PATCH` always leave the row at `status="pending"` (see
`app.services.knowledge_base_service`) and enqueue an
`embed_knowledge_document` worker job via `BackgroundTasks`
(fire-and-forget, runs after the response is sent) — see
`app.workers.rag.enqueue_kb_embedding`, which mirrors the exact
short-lived-pool enqueue idiom `app.workers.classification
.enqueue_classification` already established.

`DELETE` hard-deletes the row and its `knowledge_chunks` — see
`app.services.knowledge_base_service.delete_knowledge_base`.
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin, require_org_staff
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.knowledge_base import KnowledgeBase
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBasePatch,
    KnowledgeBaseResponse,
)
from app.services import knowledge_base_service
from app.workers.rag import enqueue_kb_embedding

router = APIRouter(prefix="/api/v1/knowledge-base", tags=["knowledge-base"])


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    background_tasks: BackgroundTasks,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBase:
    """Create a knowledge base article in the caller's organization
    (admin-only) and enqueue its `embed_knowledge_document` job."""
    knowledge_base = await knowledge_base_service.create_knowledge_base(
        session,
        scope=scope,
        title=payload.title,
        content=payload.content,
        source_type=payload.source_type,
    )
    background_tasks.add_task(
        enqueue_kb_embedding, knowledge_base.id, knowledge_base.organization_id
    )
    return knowledge_base


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_base(
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> list[KnowledgeBase]:
    """List knowledge base articles in the caller's organization
    (admin/agent only)."""
    return await knowledge_base_service.list_knowledge_base(session, scope=scope)


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    knowledge_base_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBase:
    """Fetch a single knowledge base article (admin/agent only). A
    cross-org or missing id surfaces as 404."""
    return await knowledge_base_service.get_knowledge_base(
        session, scope=scope, knowledge_base_id=knowledge_base_id
    )


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    knowledge_base_id: UUID,
    payload: KnowledgeBasePatch,
    background_tasks: BackgroundTasks,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBase:
    """Update a knowledge base article (admin-only). Any supplied field
    resets it to `status="pending"` and re-enqueues its embedding job."""
    fields = payload.model_dump(exclude_unset=True)
    knowledge_base = await knowledge_base_service.update_knowledge_base(
        session, scope=scope, knowledge_base_id=knowledge_base_id, **fields
    )
    background_tasks.add_task(
        enqueue_kb_embedding, knowledge_base.id, knowledge_base.organization_id
    )
    return knowledge_base


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: UUID,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Hard-delete a knowledge base article and its `knowledge_chunks`
    (admin-only)."""
    await knowledge_base_service.delete_knowledge_base(
        session, scope=scope, knowledge_base_id=knowledge_base_id
    )
