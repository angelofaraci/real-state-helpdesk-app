"""Integration tests for Stage 3 (RAG) real-Postgres-only behaviors,
following the same skip-stub pattern as
`tests/integration/test_classification_constraints.py`.

Static `Base.metadata` inspection (`tests/unit/test_knowledge_chunk_model.py`,
`test_knowledge_base_model.py`, `test_ticket_embedding_model.py`) proves the
CHECK constraints and unique indexes are *declared*. It does NOT prove that:

- `ck_knowledge_chunks_content_not_blank` and
  `ck_knowledge_chunks_token_count_positive` are actually enforced by
  Postgres when inserted directly (bypassing any future
  service/repository-level validation).
- `ck_knowledge_base_status_known` and
  `ck_knowledge_base_failed_requires_embedding_error` are actually enforced.
- The unique index on `ticket_embeddings.ticket_id` actually rejects a
  second INSERT for the same `ticket_id` (the idempotency guarantee a
  future re-embedding worker relies on) with an `IntegrityError` /
  `UniqueViolationError`.

These tests run against a real Postgres instance (`docker compose up -d
postgres`, `alembic upgrade head`), using the SAVEPOINT-scoped `db_session`
fixture from `tests/conftest.py` (see that module's docstring).
"""

import secrets
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole, UserStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.ticket_embedding import TicketEmbedding
from app.models.user import User
from app.services.rag_embeddings import RAG_EMBEDDING_DIM

_EMBEDDING = [0.1] * RAG_EMBEDDING_DIM


async def _make_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_user(db_session: AsyncSession, org: Organization) -> User:
    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"{uuid4()}@example.com",
        role=UserRole.TENANT,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_knowledge_base(
    db_session: AsyncSession, org: Organization
) -> KnowledgeBase:
    kb = KnowledgeBase(
        organization_id=org.id,
        title="Lease renewal policy",
        content="Some content about lease renewals.",
        status="ready",
    )
    db_session.add(kb)
    await db_session.flush()
    return kb


async def test_knowledge_chunk_rejects_blank_content_and_non_positive_token_count(
    db_session: AsyncSession,
) -> None:
    """`ck_knowledge_chunks_content_not_blank` and
    `ck_knowledge_chunks_token_count_positive` must reject blank content and
    non-positive token counts respectively, while a valid row inserts fine."""
    org = await _make_org(db_session)
    kb = await _make_knowledge_base(db_session, org)

    with pytest.raises(
        IntegrityError, match="ck_knowledge_chunks_content_not_blank"
    ):
        async with db_session.begin_nested():
            db_session.add(
                KnowledgeChunk(
                    knowledge_base_id=kb.id,
                    organization_id=org.id,
                    chunk_index=0,
                    content="   ",
                    token_count=10,
                    embedding=_EMBEDDING,
                )
            )
            await db_session.flush()

    with pytest.raises(
        IntegrityError, match="ck_knowledge_chunks_token_count_positive"
    ):
        async with db_session.begin_nested():
            db_session.add(
                KnowledgeChunk(
                    knowledge_base_id=kb.id,
                    organization_id=org.id,
                    chunk_index=0,
                    content="Valid content",
                    token_count=0,
                    embedding=_EMBEDDING,
                )
            )
            await db_session.flush()

    valid_chunk = KnowledgeChunk(
        knowledge_base_id=kb.id,
        organization_id=org.id,
        chunk_index=0,
        content="Valid content",
        token_count=10,
        embedding=_EMBEDDING,
    )
    db_session.add(valid_chunk)
    await db_session.flush()

    result = await db_session.execute(
        select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == kb.id)
    )
    chunks = result.scalars().all()
    assert {c.id for c in chunks} == {valid_chunk.id}


async def test_knowledge_base_status_check_and_failed_requires_embedding_error(
    db_session: AsyncSession,
) -> None:
    """`ck_knowledge_base_status_known` must reject an unknown status, and
    `ck_knowledge_base_failed_requires_embedding_error` must reject a
    `status='failed'` row with `embedding_error=NULL`, while a `failed` row
    with an error and a non-failed row without one both insert fine."""
    org = await _make_org(db_session)

    with pytest.raises(IntegrityError, match="ck_knowledge_base_status_known"):
        async with db_session.begin_nested():
            db_session.add(
                KnowledgeBase(
                    organization_id=org.id,
                    title="Bad status article",
                    content="Some content.",
                    status="bogus",
                )
            )
            await db_session.flush()

    with pytest.raises(
        IntegrityError,
        match="ck_knowledge_base_failed_requires_embedding_error",
    ):
        async with db_session.begin_nested():
            db_session.add(
                KnowledgeBase(
                    organization_id=org.id,
                    title="Failed without error",
                    content="Some content.",
                    status="failed",
                    embedding_error=None,
                )
            )
            await db_session.flush()

    failed_with_error = KnowledgeBase(
        organization_id=org.id,
        title="Failed with error",
        content="Some content.",
        status="failed",
        embedding_error="embedding provider timed out",
    )
    db_session.add(failed_with_error)

    ready_without_error = KnowledgeBase(
        organization_id=org.id,
        title="Ready article",
        content="Some content.",
        status="ready",
    )
    db_session.add(ready_without_error)
    await db_session.flush()

    result = await db_session.execute(
        select(KnowledgeBase).where(KnowledgeBase.organization_id == org.id)
    )
    articles = result.scalars().all()
    assert {a.id for a in articles} == {
        failed_with_error.id,
        ready_without_error.id,
    }


async def test_ticket_embedding_unique_ticket_id_rejects_duplicate_insert(
    db_session: AsyncSession,
) -> None:
    """The unique index on `ticket_embeddings.ticket_id` must reject a
    second INSERT for the same `ticket_id`, proving a re-embedding worker
    cannot silently create duplicate embeddings for one ticket."""
    org = await _make_org(db_session)
    user = await _make_user(db_session, org)

    ticket = Ticket(
        organization_id=org.id,
        user_id=user.id,
        title="Broken thermostat",
    )
    db_session.add(ticket)
    await db_session.flush()

    first_embedding = TicketEmbedding(
        ticket_id=ticket.id,
        organization_id=org.id,
        summary="Tenant reports thermostat is broken.",
        embedding=_EMBEDDING,
    )
    db_session.add(first_embedding)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ix_ticket_embeddings_ticket_id_unique"):
        async with db_session.begin_nested():
            db_session.add(
                TicketEmbedding(
                    ticket_id=ticket.id,
                    organization_id=org.id,
                    summary="Duplicate embedding for the same ticket.",
                    embedding=_EMBEDDING,
                )
            )
            await db_session.flush()

    result = await db_session.execute(
        select(TicketEmbedding).where(TicketEmbedding.ticket_id == ticket.id)
    )
    rows = result.scalars().all()
    assert {r.id for r in rows} == {first_embedding.id}
