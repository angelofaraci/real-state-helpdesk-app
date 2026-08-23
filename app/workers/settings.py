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

Stage 8 — devops, PR7. Worker metrics.

arq's `on_job_start`/`on_job_end` hooks (see `arq.worker.Worker.run_job`
in arq==0.28.0) are called with only a `ctx` dict built from
`job_id`/`job_try`/`enqueue_time`/`score` plus whatever `on_startup`
stored — arq does NOT add the function name or the job's outcome
(success vs. raised exception) to that `ctx`. That makes it impossible
to label a success/failure counter *by function* from those hooks
alone. Instead, `_with_job_metrics` wraps each job coroutine (applied
only here, in the `functions`/`cron_jobs` lists below) to record
`arq_job_success_total`/`arq_job_failure_total` labeled by function
name; `functools.wraps` preserves `__name__`/`__qualname__`/
`__wrapped__` so arq's own job-name registration (and this module's
existing identity-based tests) are unaffected.

`ctx["redis"]` (arq's own `ArqRedis` pool, injected into every hook
call — see `Worker.__init__`) IS reliably available in `on_job_start`,
so the `arq_queue_depth` gauge is refreshed there via a single `ZCARD`
against arq's own sorted-set queue key, mirroring how arq computes
queue depth internally (`Worker.queued_jobs`/`Worker.pool.zcard`).
Piggybacking the refresh on `on_job_start` — an event that already
fires once per job — avoids standing up a separate polling task; the
gauge is therefore only as fresh as the last job start, which is an
acceptable tradeoff for a queue that isn't idle for long stretches.

`start_http_server(settings.worker_metrics_port)` runs once in
`on_startup`. arq (`arq worker`) runs a single OS process per
invocation — there's no built-in multi-process pre-fork to guard
against here. If this were ever run under a process supervisor that
starts several `arq worker` processes against the same queue, each
process would try to bind its own metrics port and only the first
would succeed; deployment tooling should keep worker replication to
one process per host/port when doing that.
"""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, TypeVar

from arq import cron
from arq.connections import RedisSettings
from arq.constants import default_queue_name
from prometheus_client import Counter, Gauge, start_http_server
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.embeddings import get_embedding_provider
from app.services.rag_embeddings import get_rag_embedding_provider
from app.workers.analytics import rollup_daily_metrics
from app.workers.backup import backup_database_to_s3
from app.workers.classification import classify_ticket, sweep_pending_classifications
from app.workers.email import send_ticket_email_reply
from app.workers.rag import embed_knowledge_document, embed_resolved_ticket
from app.workers.sla import monitor_sla
from app.workers.whatsapp import process_whatsapp_message, send_whatsapp_reply

ARQ_JOB_SUCCESS = Counter(
    "arq_job_success_total",
    "Total arq jobs that completed without raising, labeled by function name.",
    labelnames=("function",),
)
ARQ_JOB_FAILURE = Counter(
    "arq_job_failure_total",
    "Total arq jobs that raised during execution, labeled by function name.",
    labelnames=("function",),
)
ARQ_QUEUE_DEPTH = Gauge(
    "arq_queue_depth",
    "Number of jobs currently queued (score-ordered set, not yet started) "
    "on the arq queue, refreshed on every job start.",
)

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _with_job_metrics(fn: _F) -> _F:
    """Wrap an arq job coroutine to record `ARQ_JOB_SUCCESS`/
    `ARQ_JOB_FAILURE`, labeled by `fn.__name__`. See the module docstring
    for why this lives here instead of `on_job_start`/`on_job_end`."""

    @functools.wraps(fn)
    async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        try:
            result = await fn(ctx, *args, **kwargs)
        except BaseException:
            ARQ_JOB_FAILURE.labels(function=fn.__name__).inc()
            raise
        else:
            ARQ_JOB_SUCCESS.labels(function=fn.__name__).inc()
            return result

    return wrapper  # type: ignore[return-value]


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(bind=engine, expire_on_commit=False)
    ctx["embedder"] = get_embedding_provider()
    ctx["rag_embedder"] = get_rag_embedding_provider()
    if settings.metrics_enabled:
        start_http_server(settings.worker_metrics_port)


async def on_job_start(ctx: dict[str, Any]) -> None:
    """Runs once per job, right before it executes. Also refreshes the
    queue-depth gauge here — see the module docstring for why this piggy-
    backs on job start rather than a dedicated poller."""
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        depth = await redis.zcard(default_queue_name)
        ARQ_QUEUE_DEPTH.set(depth)
    except Exception:
        # A metrics refresh must never take down a job.
        pass


async def on_shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


class WorkerSettings:
    """arq `Worker` configuration for the classification worker process."""

    functions = [
        _with_job_metrics(classify_ticket),
        _with_job_metrics(embed_knowledge_document),
        _with_job_metrics(embed_resolved_ticket),
        _with_job_metrics(send_ticket_email_reply),
        _with_job_metrics(process_whatsapp_message),
        _with_job_metrics(send_whatsapp_reply),
    ]
    cron_jobs = [
        cron(
            _with_job_metrics(sweep_pending_classifications),
            minute=set(range(0, 60, 5)),
            run_at_startup=False,
        ),
        cron(_with_job_metrics(monitor_sla), minute=set(range(0, 60, 5)), run_at_startup=False),
        # Stage 7 — analytics, PR3. Nightly at 00:30 UTC, well clear of the
        # top-of-hour 5-minute cadence the two jobs above run on — recomputes
        # the last `LOOKBACK_DAYS` completed org-local days for every org.
        cron(_with_job_metrics(rollup_daily_metrics), hour={0}, minute={30}, run_at_startup=False),
        # Stage 8 — devops, PR8. Nightly at 03:00 UTC, well clear of the
        # analytics rollup above — `pg_dump`s the database, gzips it, and
        # uploads it to `settings.backup_s3_bucket` (no-ops when unset).
        cron(
            _with_job_metrics(backup_database_to_s3), hour={3}, minute={0}, run_at_startup=False
        ),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)
    on_job_start = staticmethod(on_job_start)
    max_tries = 3
