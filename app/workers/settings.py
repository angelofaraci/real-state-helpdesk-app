"""arq `WorkerSettings` for the stage-2 ticket-classification worker
process (`arq app.workers.settings.WorkerSettings`).

`on_startup` builds the two pieces of shared, expensive-to-construct state
every job needs and stores them on `ctx` for `app.workers.classification`
to pick up:

- `ctx["session_factory"]`: an `async_sessionmaker` bound to its own engine
  (deliberately separate from `app.core.session`'s FastAPI-request-scoped
  one — a long-lived worker process has a different connection-pool
  lifecycle than a request handler).
- `ctx["embedder"]`: the `EmbeddingProvider` selected by
  `settings.embedding_provider`, built once per worker process rather than
  per job.
- `ctx["rag_embedder"]`: the Stage-3 RAG `RagEmbeddingProvider` selected by
  `settings.rag_embedding_provider` (see `app.services.rag_embeddings`) —
  deliberately separate from `ctx["embedder"]` above (different model,
  different dimension), built once per worker process for
  `app.workers.rag.embed_knowledge_document` and
  `app.workers.rag.embed_resolved_ticket` to use.

`ctx["llm_client"]` is deliberately NOT set here: `embed_resolved_ticket`
looks it up via `ctx.get("llm_client")` and, when absent (the real
`on_startup` case), constructs its own `openai.AsyncOpenAI` client per
call from `settings.openai_api_key` — the same injectable-client pattern
`app.services.llm_fallback.classify_with_llm` already uses, so tests can
inject a fake client without this module needing to build one upfront.

`on_shutdown` disposes the engine so the process doesn't leak connections
on exit.
"""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.embeddings import get_embedding_provider
from app.services.rag_embeddings import get_rag_embedding_provider
from app.workers.classification import classify_ticket, sweep_pending_classifications
from app.workers.email import send_ticket_email_reply
from app.workers.rag import embed_knowledge_document, embed_resolved_ticket
from app.workers.sla import monitor_sla
from app.workers.whatsapp import process_whatsapp_message, send_whatsapp_reply


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(bind=engine, expire_on_commit=False)
    ctx["embedder"] = get_embedding_provider()
    ctx["rag_embedder"] = get_rag_embedding_provider()


async def on_shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


class WorkerSettings:
    """arq `Worker` configuration for the classification worker process."""

    functions = [
        classify_ticket,
        embed_knowledge_document,
        embed_resolved_ticket,
        send_ticket_email_reply,
        process_whatsapp_message,
        send_whatsapp_reply,
    ]
    cron_jobs = [
        cron(sweep_pending_classifications, minute=set(range(0, 60, 5)), run_at_startup=False),
        cron(monitor_sla, minute=set(range(0, 60, 5)), run_at_startup=False),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)
    max_tries = 3
