"""Unit tests for `app.workers.settings.WorkerSettings` — the arq
`Worker` wiring for stage-2 ticket classification: registers
`classify_ticket` as a runnable job and schedules
`sweep_pending_classifications` via a cron job.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.cron import CronJob

from app.workers import classification, email, rag
from app.workers.settings import WorkerSettings


def _function_names() -> list[str]:
    names = []
    for entry in WorkerSettings.functions:
        # arq accepts either a bare coroutine or an `arq.worker.Function`
        # (the shape `arq.func(...)` returns); both expose `.coroutine`.
        coroutine = getattr(entry, "coroutine", entry)
        names.append(coroutine.__name__)
    return names


def test_classify_ticket_is_registered_as_a_runnable_function() -> None:
    assert "classify_ticket" in _function_names()


def test_functions_reference_the_classification_module_callables() -> None:
    coroutines = [getattr(entry, "coroutine", entry) for entry in WorkerSettings.functions]
    assert classification.classify_ticket in coroutines


def test_embed_resolved_ticket_is_registered_as_a_runnable_function() -> None:
    assert "embed_resolved_ticket" in _function_names()


def test_functions_reference_the_rag_module_embed_resolved_ticket_callable() -> None:
    coroutines = [getattr(entry, "coroutine", entry) for entry in WorkerSettings.functions]
    assert rag.embed_resolved_ticket in coroutines


def test_send_ticket_email_reply_is_registered_as_a_runnable_function() -> None:
    assert "send_ticket_email_reply" in _function_names()


def test_functions_reference_the_email_module_send_ticket_email_reply_callable() -> None:
    coroutines = [getattr(entry, "coroutine", entry) for entry in WorkerSettings.functions]
    assert email.send_ticket_email_reply in coroutines


def test_cron_jobs_include_the_sweep() -> None:
    assert WorkerSettings.cron_jobs, "expected at least one cron job"
    for job in WorkerSettings.cron_jobs:
        assert isinstance(job, CronJob)

    sweep_names = [job.coroutine.__name__ for job in WorkerSettings.cron_jobs]
    assert "sweep_pending_classifications" in sweep_names


def test_redis_settings_is_configured() -> None:
    assert isinstance(WorkerSettings.redis_settings, RedisSettings)


def test_on_startup_and_on_shutdown_hooks_are_defined() -> None:
    assert callable(WorkerSettings.on_startup)
    assert callable(WorkerSettings.on_shutdown)


async def test_on_startup_populates_ctx_with_session_factory_and_embedder() -> None:
    ctx: dict = {}
    await WorkerSettings.on_startup(ctx)

    assert "session_factory" in ctx
    assert callable(ctx["session_factory"])
    assert "embedder" in ctx
    assert hasattr(ctx["embedder"], "embed")

    await WorkerSettings.on_shutdown(ctx)
