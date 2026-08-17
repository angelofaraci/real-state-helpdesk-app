"""KnowledgeChunk model — a chunked, embedded slice of a knowledge base
article's content, used for RAG retrieval.

`RAG_EMBEDDING_DIM` is a module-local constant for stage 3 PR1. Once
`app.services.rag_embeddings` exists (a later PR in this change), the
embedding provider's dimension constant should become the single source of
truth and this module (and `ticket_embedding.py`) should import from it
instead of redefining it locally.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

RAG_EMBEDDING_DIM = 768


class KnowledgeChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(RAG_EMBEDDING_DIM), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "btrim(content) <> ''", name="ck_knowledge_chunks_content_not_blank"
        ),
        CheckConstraint(
            "token_count > 0", name="ck_knowledge_chunks_token_count_positive"
        ),
        Index(
            "ix_knowledge_chunks_kb_id_chunk_index",
            "knowledge_base_id",
            "chunk_index",
            unique=True,
        ),
        Index("ix_knowledge_chunks_organization_id", "organization_id"),
    )
