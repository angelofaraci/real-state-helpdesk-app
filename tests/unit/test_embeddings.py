"""Unit tests for `app.services.embeddings` — the embedding-provider
factory and its two implementations.

`SentenceTransformerEmbeddingProvider` lazy-imports `sentence_transformers`
(an optional `[ml]` dependency) so importing this module never requires
it. `OpenAIEmbeddingProvider`'s actual API call is made through an
injected client, so no real network call happens in these tests.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.embeddings import (
    OpenAIEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    get_embedding_provider,
)


def test_factory_selects_sentence_transformer_provider_for_local() -> None:
    provider = get_embedding_provider(embedding_provider="local")

    assert isinstance(provider, SentenceTransformerEmbeddingProvider)


def test_factory_selects_openai_provider_for_openai() -> None:
    provider = get_embedding_provider(embedding_provider="openai", openai_api_key="sk-test")

    assert isinstance(provider, OpenAIEmbeddingProvider)


@pytest.mark.asyncio
async def test_openai_provider_embeds_via_injected_client_without_network_call() -> None:
    fake_response = MagicMock()
    fake_response.data = [
        MagicMock(embedding=[0.1, 0.2]),
        MagicMock(embedding=[0.3, 0.4]),
    ]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_response)

    provider = OpenAIEmbeddingProvider(client=fake_client)

    result = await provider.embed(["hello", "world"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    fake_client.embeddings.create.assert_awaited_once_with(
        model="text-embedding-3-small", input=["hello", "world"]
    )


@pytest.mark.asyncio
async def test_sentence_transformer_provider_embeds_via_injected_model() -> None:
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.5, 0.6], [0.7, 0.8]]

    provider = SentenceTransformerEmbeddingProvider(model=fake_model)

    result = await provider.embed(["hello", "world"])

    assert result == [[0.5, 0.6], [0.7, 0.8]]
    fake_model.encode.assert_called_once_with(["hello", "world"])
