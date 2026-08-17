"""Embedding providers for stage-3 RAG (knowledge base chunks and ticket
similarity search).

This module is deliberately INDEPENDENT from `app.services.embeddings`
(stage-2 ticket classification): different model, different dimension
(768 here vs stage-2's 384), and it must never import from that module (nor
vice versa) so the two embedding pipelines can evolve independently.

Two implementations of the `RagEmbeddingProvider` protocol:

- `SentenceTransformerRagEmbeddingProvider` — local/dev, backed by the
  `sentence-transformers` package (an optional `[ml]` dependency),
  defaulting to the 768-dim `all-mpnet-base-v2` model.
  `sentence_transformers` is lazy-imported (matching
  `app.services.embeddings`'s existing convention) so importing this
  module never requires it unless the local provider is actually
  constructed without an injected model.
- `OpenAIRagEmbeddingProvider` — production, backed by OpenAI's
  `text-embedding-3-small` model, explicitly requesting `dimensions=768`
  on every call so its output matches the DB column's fixed dimension.

`get_rag_embedding_provider()` selects between the two based on
`settings.rag_embedding_provider` ("local" -> sentence-transformers,
anything else, e.g. "openai" -> OpenAI).

`_require_dim()` is the runtime guard: every provider validates its output
against `RAG_EMBEDDING_DIM` before returning, since a silently wrong
dimension would fail only much later at the DB insert (or worse, corrupt
`pgvector` similarity search results if the column were ever loosened).
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.core.config import get_settings

RAG_EMBEDDING_DIM = 768

OPENAI_RAG_EMBEDDING_MODEL = "text-embedding-3-small"
SENTENCE_TRANSFORMER_RAG_MODEL = "all-mpnet-base-v2"


class RagEmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RagEmbeddingDimensionError(RuntimeError):
    """Raised when an embedding provider returns a vector whose dimension
    does not match `RAG_EMBEDDING_DIM`."""


def _require_dim(vectors: list[list[float]]) -> list[list[float]]:
    for vector in vectors:
        if len(vector) != RAG_EMBEDDING_DIM:
            raise RagEmbeddingDimensionError(
                f"RAG embedding must be {RAG_EMBEDDING_DIM}-dim, got {len(vector)}"
            )
    return vectors


class SentenceTransformerRagEmbeddingProvider:
    """Local/dev embedding provider backed by `sentence-transformers`,
    defaulting to the 768-dim `all-mpnet-base-v2` model.

    `sentence_transformers` is only imported when no `model` is injected
    and `embed()` is actually called, so constructing/importing this
    class does not require the `[ml]` optional dependency to be
    installed unless the local provider is actually used.
    """

    def __init__(
        self, *, model=None, model_name: str = SENTENCE_TRANSFORMER_RAG_MODEL
    ) -> None:
        self._model = model
        self._model_name = model_name

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy import

            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._get_model()
        embeddings = model.encode(list(texts))
        return _require_dim([list(vector) for vector in embeddings])


class OpenAIRagEmbeddingProvider:
    """Production embedding provider backed by OpenAI's
    `text-embedding-3-small` model, explicitly requesting `dimensions=768`
    so its output matches the fixed `RAG_EMBEDDING_DIM` DB column width.

    `client` must be (or behave like) an `openai.AsyncOpenAI` instance;
    it is injected so tests never make a real network call.
    """

    def __init__(self, *, client) -> None:
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=OPENAI_RAG_EMBEDDING_MODEL,
            input=list(texts),
            dimensions=RAG_EMBEDDING_DIM,
        )
        return _require_dim([list(item.embedding) for item in response.data])


def get_rag_embedding_provider(
    *, provider: str | None = None, openai_api_key: str | None = None
) -> RagEmbeddingProvider:
    """Return the `RagEmbeddingProvider` selected by
    `settings.rag_embedding_provider` ("local" -> sentence-transformers,
    anything else -> OpenAI).

    `provider`/`openai_api_key` may be passed explicitly (used by tests);
    otherwise they default to the current `Settings`.
    """
    settings = get_settings()
    provider_name = provider if provider is not None else settings.rag_embedding_provider

    if provider_name == "local":
        return SentenceTransformerRagEmbeddingProvider()

    import openai

    api_key = openai_api_key if openai_api_key is not None else settings.openai_api_key
    client = openai.AsyncOpenAI(api_key=api_key)
    return OpenAIRagEmbeddingProvider(client=client)
