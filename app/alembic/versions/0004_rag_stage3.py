"""stage 3 RAG foundation: knowledge_base, knowledge_chunks,
ticket_embeddings, and messages.based_on_suggestion_id

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RAG_EMBEDDING_DIM = 768


def upgrade() -> None:
    # pgvector extension already created in 0001_initial.py.

    op.create_table(
        "knowledge_base",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("embedding_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "btrim(title) <> ''", name="ck_knowledge_base_title_not_blank"
        ),
        sa.CheckConstraint(
            "btrim(content) <> ''", name="ck_knowledge_base_content_not_blank"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_knowledge_base_status_known",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR embedding_error IS NOT NULL",
            name="ck_knowledge_base_failed_requires_embedding_error",
        ),
    )
    op.create_index(
        "ix_knowledge_base_organization_id", "knowledge_base", ["organization_id"]
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "knowledge_base_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_base.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(RAG_EMBEDDING_DIM), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "btrim(content) <> ''", name="ck_knowledge_chunks_content_not_blank"
        ),
        sa.CheckConstraint(
            "token_count > 0", name="ck_knowledge_chunks_token_count_positive"
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_kb_id_chunk_index",
        "knowledge_chunks",
        ["knowledge_base_id", "chunk_index"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_chunks_organization_id", "knowledge_chunks", ["organization_id"]
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "ticket_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(RAG_EMBEDDING_DIM), nullable=False),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "btrim(summary) <> ''", name="ck_ticket_embeddings_summary_not_blank"
        ),
    )
    op.create_index(
        "ix_ticket_embeddings_ticket_id_unique",
        "ticket_embeddings",
        ["ticket_id"],
        unique=True,
    )
    op.create_index(
        "ix_ticket_embeddings_organization_id", "ticket_embeddings", ["organization_id"]
    )
    op.execute(
        "CREATE INDEX ix_ticket_embeddings_embedding_hnsw ON ticket_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.add_column(
        "messages",
        sa.Column(
            "based_on_suggestion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_messages_based_on_suggestion_not_self",
        "messages",
        "based_on_suggestion_id IS NULL OR based_on_suggestion_id <> id",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_messages_based_on_suggestion_not_self", "messages", type_="check"
    )
    op.drop_column("messages", "based_on_suggestion_id")

    op.execute("DROP INDEX ix_ticket_embeddings_embedding_hnsw")
    op.drop_index("ix_ticket_embeddings_organization_id", table_name="ticket_embeddings")
    op.drop_index("ix_ticket_embeddings_ticket_id_unique", table_name="ticket_embeddings")
    op.drop_table("ticket_embeddings")

    op.execute("DROP INDEX ix_knowledge_chunks_embedding_hnsw")
    op.drop_index("ix_knowledge_chunks_organization_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_kb_id_chunk_index", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_base_organization_id", table_name="knowledge_base")
    op.drop_table("knowledge_base")
