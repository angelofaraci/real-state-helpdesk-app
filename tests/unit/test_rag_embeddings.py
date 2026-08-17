"""Unit tests for `app.services.rag_embeddings`.

Isolation invariant: this module must NOT import from
`app.services.embeddings` (stage-2 classification embeddings), and vice
versa — the two providers are independent (different model, different
dimension: 768 vs stage-2's 384).

`SentenceTransformerRagEmbeddingProvider` lazy-imports `sentence_transformers`
(an optional `[ml]` dependency), matching `app.services.embeddings`'s
existing convention, so importing this module never requires it.
`OpenAIRagEmbeddingProvider`'s actual API call goes through an injected
client, so no real network call happens in these tests.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.rag_embeddings import (
    RAG_EMBEDDING_DIM,
    OpenAIRagEmbeddingProvider,
    RagEmbeddingDimensionError,
    SentenceTransformerRagEmbeddingProvider,
    _require_dim,
    get_rag_embedding_provider,
)


def test_rag_embedding_dim_is_768() -> None:
    assert RAG_EMBEDDING_DIM == 768


def test_require_dim_accepts_vectors_matching_768() -> None:
    vectors = [[0.0] * 768, [1.0] * 768]

    result = _require_dim(vectors)

    assert result == vectors


def test_require_dim_rejects_vector_with_wrong_dimension() -> None:
    vectors = [[0.0] * 768, [1.0] * 384]

    with pytest.raises(RagEmbeddingDimensionError, match="768"):
        _require_dim(vectors)


def test_require_dim_rejects_empty_vector() -> None:
    with pytest.raises(RagEmbeddingDimensionError):
        _require_dim([[]])


def test_factory_selects_sentence_transformer_provider_for_local() -> None:
    provider = get_rag_embedding_provider(provider="local")

    assert isinstance(provider, SentenceTransformerRagEmbeddingProvider)


def test_factory_selects_openai_provider_for_openai() -> None:
    provider = get_rag_embedding_provider(provider="openai", openai_api_key="sk-test")

    assert isinstance(provider, OpenAIRagEmbeddingProvider)


@pytest.mark.asyncio
async def test_openai_provider_requests_768_dimensions_explicitly() -> None:
    fake_response = MagicMock()
    fake_response.data = [MagicMock(embedding=[0.1] * 768)]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_response)

    provider = OpenAIRagEmbeddingProvider(client=fake_client)

    result = await provider.embed(["hello"])

    assert result == [[0.1] * 768]
    fake_client.embeddings.create.assert_awaited_once_with(
        model="text-embedding-3-small", input=["hello"], dimensions=768
    )


@pytest.mark.asyncio
async def test_openai_provider_raises_on_wrong_dimension_response() -> None:
    fake_response = MagicMock()
    fake_response.data = [MagicMock(embedding=[0.1] * 384)]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_response)

    provider = OpenAIRagEmbeddingProvider(client=fake_client)

    with pytest.raises(RagEmbeddingDimensionError):
        await provider.embed(["hello"])


@pytest.mark.asyncio
async def test_sentence_transformer_provider_embeds_via_injected_model() -> None:
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.2] * 768]

    provider = SentenceTransformerRagEmbeddingProvider(model=fake_model)

    result = await provider.embed(["hello"])

    assert result == [[0.2] * 768]
    fake_model.encode.assert_called_once_with(["hello"])


@pytest.mark.asyncio
async def test_sentence_transformer_provider_raises_on_wrong_dimension_output() -> None:
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.2] * 384]

    provider = SentenceTransformerRagEmbeddingProvider(model=fake_model)

    with pytest.raises(RagEmbeddingDimensionError):
        await provider.embed(["hello"])


def test_rag_embeddings_module_does_not_import_stage2_embeddings_module() -> None:
    """Static isolation check: `rag_embeddings.py` must never import
    `app.services.embeddings`, so the two providers stay independent."""
    source = Path("app/services/rag_embeddings.py").read_text()
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        module == "app.services.embeddings" or module.startswith("app.services.embeddings.")
        for module in imported_modules
    )
