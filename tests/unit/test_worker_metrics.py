"""Unit tests for the Prometheus worker metrics added in stage 8, PR7
(`app.workers.settings`): per-function job success/failure counters and
the queue-depth gauge.

Why a decorator instead of `on_job_end` alone: arq's `on_job_start`/
`on_job_end` worker hooks are called with only a `ctx` dict built from
`job_id`/`job_try`/`enqueue_time`/`score` (see `arq.worker.Worker.run_job`
in arq==0.28.0) — arq does NOT add the function name or the job's
outcome (success vs. raised exception) to that `ctx`, so those hooks
alone cannot label a success/failure counter *by function*. Wrapping
each job coroutine with `_with_job_metrics` (applied only in
`app.workers.settings`, preserving `__name__`/`__qualname__`/
`__wrapped__` via `functools.wraps` so arq's own job registration is
unaffected) is what actually records these counters; `on_job_start`
handles the queue-depth gauge, which *is* derivable from the shared
`ctx["redis"]` connection arq injects into every hook call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from app.workers.settings import _with_job_metrics, on_job_start


def _sample(name: str, labels: dict[str, str]) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


@pytest.mark.asyncio
async def test_with_job_metrics_increments_success_counter_on_success() -> None:
    async def fake_job(ctx: dict[str, Any], *, ok: bool = True) -> str:
        return "done"

    wrapped = _with_job_metrics(fake_job)
    before = _sample("arq_job_success_total", {"function": "fake_job"}) or 0.0

    result = await wrapped({}, ok=True)

    assert result == "done"
    after = _sample("arq_job_success_total", {"function": "fake_job"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_with_job_metrics_increments_failure_counter_and_reraises() -> None:
    async def failing_job(ctx: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    wrapped = _with_job_metrics(failing_job)
    before = _sample("arq_job_failure_total", {"function": "failing_job"}) or 0.0

    with pytest.raises(RuntimeError, match="boom"):
        await wrapped({})

    after = _sample("arq_job_failure_total", {"function": "failing_job"})
    assert after == before + 1


def test_with_job_metrics_preserves_function_identity_for_arq_registration() -> None:
    async def some_job(ctx: dict[str, Any]) -> None:
        return None

    wrapped = _with_job_metrics(some_job)

    assert wrapped.__name__ == "some_job"
    assert wrapped.__wrapped__ is some_job


@pytest.mark.asyncio
async def test_on_job_start_refreshes_queue_depth_gauge_from_redis() -> None:
    fake_redis = AsyncMock()
    fake_redis.zcard.return_value = 7

    await on_job_start({"redis": fake_redis})

    fake_redis.zcard.assert_awaited_once()
    assert _sample("arq_queue_depth", {}) == 7.0


@pytest.mark.asyncio
async def test_on_job_start_does_not_raise_when_redis_is_absent() -> None:
    # `ctx` in a unit test / bare hook invocation may not carry `redis` —
    # the hook must not blow up the worker over a metrics refresh.
    await on_job_start({})
