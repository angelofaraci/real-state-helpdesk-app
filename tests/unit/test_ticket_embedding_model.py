"""Unit tests for the `TicketEmbedding` ORM model.

Pure `Base.metadata` inspection — no database connection required. Unique
constraint *enforcement* (idempotency: one embedding row per ticket) by real
Postgres is covered separately in `tests/integration/test_rag_constraints.py`.
"""

from pgvector.sqlalchemy import Vector

from app.models import Base
from app.models.ticket_embedding import RAG_EMBEDDING_DIM


def _check_sqltexts(table) -> set[str]:
    return {
        str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_ticket_embeddings_table_registered_on_metadata() -> None:
    assert "ticket_embeddings" in Base.metadata.tables


def test_ticket_embeddings_columns_nullability() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]

    assert ticket_embeddings.columns["ticket_id"].nullable is False
    assert ticket_embeddings.columns["organization_id"].nullable is False
    assert ticket_embeddings.columns["summary"].nullable is False
    assert ticket_embeddings.columns["embedding"].nullable is False
    assert ticket_embeddings.columns["model_used"].nullable is True


def test_ticket_embeddings_embedding_column_is_vector_768() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]
    embedding_column = ticket_embeddings.columns["embedding"]

    assert isinstance(embedding_column.type, Vector)
    assert embedding_column.type.dim == RAG_EMBEDDING_DIM == 768


def test_ticket_embeddings_summary_not_blank_check_constraint_present() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]

    check_sqltexts = _check_sqltexts(ticket_embeddings)
    assert any("summary" in text and "btrim" in text for text in check_sqltexts)


def test_ticket_embeddings_ticket_id_unique_index_present() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]

    unique_indexes = [ix for ix in ticket_embeddings.indexes if ix.unique]
    assert any(
        {c.name for c in ix.columns} == {"ticket_id"} for ix in unique_indexes
    ), "ticket_embeddings must have a unique index on ticket_id for insert idempotency"


def test_ticket_embeddings_foreign_keys() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]

    ticket_fk_targets = {
        fk.target_fullname for fk in ticket_embeddings.columns["ticket_id"].foreign_keys
    }
    assert ticket_fk_targets == {"tickets.id"}

    org_fk_targets = {
        fk.target_fullname for fk in ticket_embeddings.columns["organization_id"].foreign_keys
    }
    assert org_fk_targets == {"organizations.id"}
