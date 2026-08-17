"""Unit tests for the `KnowledgeChunk` ORM model.

Like `test_models_metadata.py`, these inspect `Base.metadata` directly (pure
Python object graph) — no database connection required. CHECK constraint
*enforcement* by real Postgres is covered separately in
`tests/integration/test_rag_constraints.py` (skip-stubbed until a live
Postgres integration harness exists).
"""

from pgvector.sqlalchemy import Vector

from app.models import Base
from app.models.knowledge_chunk import RAG_EMBEDDING_DIM


def _check_sqltexts(table) -> set[str]:
    return {
        str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_knowledge_chunks_table_registered_on_metadata() -> None:
    assert "knowledge_chunks" in Base.metadata.tables


def test_knowledge_chunks_content_not_blank_check_constraint_present() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    check_sqltexts = _check_sqltexts(knowledge_chunks)
    assert any("content" in text and "btrim" in text for text in check_sqltexts)


def test_knowledge_chunks_token_count_positive_check_constraint_present() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    check_sqltexts = _check_sqltexts(knowledge_chunks)
    assert any("token_count" in text and ">" in text for text in check_sqltexts)


def test_knowledge_chunks_embedding_column_is_vector_768() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]
    embedding_column = knowledge_chunks.columns["embedding"]

    assert isinstance(embedding_column.type, Vector)
    assert embedding_column.type.dim == RAG_EMBEDDING_DIM == 768
    assert embedding_column.nullable is False


def test_knowledge_chunks_content_and_token_count_are_not_nullable() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    assert knowledge_chunks.columns["content"].nullable is False
    assert knowledge_chunks.columns["token_count"].nullable is False


def test_knowledge_chunks_foreign_keys() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    kb_fk_targets = {
        fk.target_fullname for fk in knowledge_chunks.columns["knowledge_base_id"].foreign_keys
    }
    assert kb_fk_targets == {"knowledge_base.id"}

    org_fk_targets = {
        fk.target_fullname for fk in knowledge_chunks.columns["organization_id"].foreign_keys
    }
    assert org_fk_targets == {"organizations.id"}


def test_knowledge_chunks_unique_index_on_knowledge_base_id_and_chunk_index() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    unique_indexes = [ix for ix in knowledge_chunks.indexes if ix.unique]
    assert any(
        {c.name for c in ix.columns} == {"knowledge_base_id", "chunk_index"}
        for ix in unique_indexes
    )


def test_knowledge_chunks_organization_id_index_present() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]

    assert any(
        {c.name for c in ix.columns} == {"organization_id"}
        for ix in knowledge_chunks.indexes
    )
