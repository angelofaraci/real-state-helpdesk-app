"""Regression test for the PR3 refactor that centralizes `RAG_EMBEDDING_DIM`
in `app.services.rag_embeddings` (previously `knowledge_chunk.py` and
`ticket_embedding.py` each defined it locally as an interim measure in
PR1). Both models must still expose a 768-dim `Vector` embedding column,
and both must now source `RAG_EMBEDDING_DIM` from the same module.
"""

from pgvector.sqlalchemy import Vector

from app.models import Base
import app.models.knowledge_chunk as knowledge_chunk_module
import app.models.ticket_embedding as ticket_embedding_module
from app.services.rag_embeddings import RAG_EMBEDDING_DIM


def test_rag_embedding_dim_is_768() -> None:
    assert RAG_EMBEDDING_DIM == 768


def test_knowledge_chunk_module_reexports_the_centralized_constant() -> None:
    assert knowledge_chunk_module.RAG_EMBEDDING_DIM is RAG_EMBEDDING_DIM


def test_ticket_embedding_module_reexports_the_centralized_constant() -> None:
    assert ticket_embedding_module.RAG_EMBEDDING_DIM is RAG_EMBEDDING_DIM


def test_knowledge_chunks_embedding_column_is_still_vector_768_after_refactor() -> None:
    knowledge_chunks = Base.metadata.tables["knowledge_chunks"]
    embedding_column = knowledge_chunks.columns["embedding"]

    assert isinstance(embedding_column.type, Vector)
    assert embedding_column.type.dim == 768


def test_ticket_embeddings_embedding_column_is_still_vector_768_after_refactor() -> None:
    ticket_embeddings = Base.metadata.tables["ticket_embeddings"]
    embedding_column = ticket_embeddings.columns["embedding"]

    assert isinstance(embedding_column.type, Vector)
    assert embedding_column.type.dim == 768
