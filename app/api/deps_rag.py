"""FastAPI dependency providers for the Stage 3 RAG API layer (PR7).

These wrap the request-scoped construction of a `RagEmbeddingProvider`
(`app.services.rag_embeddings.get_rag_embedding_provider`, already used by
`app.workers.rag`'s ingestion/summary jobs) and an `openai.AsyncOpenAI`
client (the exact same injectable-client pattern
`app.workers.rag`/`app.services.llm_fallback` already establish for the
worker/classification paths) as ordinary `Depends(...)` callables, so
`GET /tickets/{ticket_id}/suggested-response` can call
`app.services.rag.suggest_response` the same way every other route injects
`session`/`scope` — and so tests can override both via
`app.dependency_overrides`, exactly like `get_session`/`get_principal`.

There is no FastAPI-layer caching/pooling here: a fresh provider/client is
constructed per request, matching the request-scoped lifetime of every
other `Depends(...)` in this app (e.g. `get_session`).
"""

from __future__ import annotations

import openai

from app.core.config import get_settings
from app.services.rag_embeddings import RagEmbeddingProvider, get_rag_embedding_provider


def get_rag_embedder() -> RagEmbeddingProvider:
    """Return the `RagEmbeddingProvider` selected by
    `settings.rag_embedding_provider`, same selection
    `app.workers.rag.embed_knowledge_document` uses."""
    return get_rag_embedding_provider()


def get_llm_client() -> openai.AsyncOpenAI:
    """Return an `openai.AsyncOpenAI` client built from
    `settings.openai_api_key`, same injectable-client pattern
    `app.workers.rag`/`app.services.llm_fallback` use for their own LLM
    calls."""
    settings = get_settings()
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)
