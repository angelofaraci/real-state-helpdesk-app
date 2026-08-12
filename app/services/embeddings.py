"""Embedding providers for stage-2 ticket classification.

Two implementations of the `EmbeddingProvider` protocol
(`app.services.classifier.EmbeddingProvider`):

- `SentenceTransformerEmbeddingProvider` — local/dev, backed by the
  `sentence-transformers` package (an optional `[ml]` dependency).
  `sentence_transformers` is lazy-imported so importing this module
  never requires it unless the local provider is actually constructed
  without an injected model.
- `OpenAIEmbeddingProvider` — production, backed by OpenAI's
  `text-embedding-3-small` model. The actual API call goes through an
  injected `openai.AsyncOpenAI`-compatible client, so it is fully
  mockable in tests (no real network call).

`get_embedding_provider()` selects between the two based on
`settings.embedding_provider` ("local" -> sentence-transformers,
anything else, e.g. "openai" -> OpenAI).
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.core.config import get_settings

OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbeddingProvider:
    """Local/dev embedding provider backed by `sentence-transformers`.

    `sentence_transformers` is only imported when no `model` is injected
    and `embed()` is actually called, so constructing/importing this
    class does not require the `[ml]` optional dependency to be
    installed unless the local provider is actually used.
    """

    def __init__(self, *, model=None, model_name: str = "all-MiniLM-L6-v2") -> None:
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
        return [list(vector) for vector in embeddings]


class OpenAIEmbeddingProvider:
    """Production embedding provider backed by OpenAI's
    `text-embedding-3-small` model.

    `client` must be (or behave like) an `openai.AsyncOpenAI` instance;
    it is injected so tests never make a real network call.
    """

    def __init__(self, *, client) -> None:
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL, input=list(texts)
        )
        return [list(item.embedding) for item in response.data]


def get_embedding_provider(
    *, embedding_provider: str | None = None, openai_api_key: str | None = None
) -> EmbeddingProvider:
    """Return the `EmbeddingProvider` selected by `settings.embedding_provider`
    ("local" -> sentence-transformers, anything else -> OpenAI).

    `embedding_provider`/`openai_api_key` may be passed explicitly (used
    by tests); otherwise they default to the current `Settings`.
    """
    settings = get_settings()
    provider_name = embedding_provider if embedding_provider is not None else settings.embedding_provider

    if provider_name == "local":
        return SentenceTransformerEmbeddingProvider()

    import openai

    api_key = openai_api_key if openai_api_key is not None else settings.openai_api_key
    client = openai.AsyncOpenAI(api_key=api_key)
    return OpenAIEmbeddingProvider(client=client)
