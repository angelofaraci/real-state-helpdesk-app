"""TicketEmbedding model — a single embedding vector summarizing a ticket's
conversation, used for RAG similarity search over past tickets.

`RAG_EMBEDDING_DIM` is a module-local constant for stage 3 PR1 — see the
note in `knowledge_chunk.py`.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

RAG_EMBEDDING_DIM = 768


class TicketEmbedding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ticket_embeddings"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(RAG_EMBEDDING_DIM), nullable=False
    )
    model_used: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "btrim(summary) <> ''", name="ck_ticket_embeddings_summary_not_blank"
        ),
        Index("ix_ticket_embeddings_ticket_id_unique", "ticket_id", unique=True),
        Index("ix_ticket_embeddings_organization_id", "organization_id"),
    )
