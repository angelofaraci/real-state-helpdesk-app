"""KnowledgeBase model — a source article/document ingested for RAG,
chunked and embedded asynchronously via `knowledge_chunks`."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

KNOWLEDGE_BASE_STATUSES = ("pending", "processing", "ready", "failed")


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_base"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="ck_knowledge_base_title_not_blank"),
        CheckConstraint(
            "btrim(content) <> ''", name="ck_knowledge_base_content_not_blank"
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_knowledge_base_status_known",
        ),
        CheckConstraint(
            "status <> 'failed' OR embedding_error IS NOT NULL",
            name="ck_knowledge_base_failed_requires_embedding_error",
        ),
    )
