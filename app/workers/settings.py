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
from app.workers.classification import classify_ticket, sweep_pending_classifications


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(bind=engine, expire_on_commit=False)
    ctx["embedder"] = get_embedding_provider()


async def on_shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


class WorkerSettings:
    """arq `Worker` configuration for the classification worker process."""

    functions = [classify_ticket]
    cron_jobs = [
        cron(sweep_pending_classifications, minute=set(range(0, 60, 5)), run_at_startup=False)
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)
    max_tries = 3
