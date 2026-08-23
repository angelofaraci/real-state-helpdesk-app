"""Unit tests for `app.workers.analytics` — the analytics-rollup cron job
(`rollup_daily_metrics`, stage 7 — analytics, PR3/PR6).

`AsyncSession` is mocked, following the exact pattern established by
`tests/unit/test_worker_sla.py`: the worker opens/commits its own session
via an injected `ctx["session_factory"]`, and each per-org-day
metric-family attempt runs inside `session.begin_nested()` (a SAVEPOINT),
so the fake session here also stubs `begin_nested()` as an async context
manager.

`analytics_service.tickets_family_metrics`/`sla_family_metrics`/
`chat_family_metrics`/`classifier_family_metrics`/`rag_family_metrics`/
`upsert_daily_metrics` (all proven independently by
`tests/unit/test_analytics_service.py` and
`tests/integration/test_analytics_service_pg.py`) are monkeypatched on the
service module rather than re-exercised here — this module owns proving
the WORKER's own control flow: organization iteration, the
`lookback_days=2` completed-day window, per-family SAVEPOINT fault
isolation, and cron registration. Every test monkeypatches ALL FIVE family
functions (even ones it does not assert on) so the worker's real
`_FAMILY_FUNCTION_NAMES` dispatch never hits real unmocked SQL against a
fake `AsyncMock` session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from arq.cron import CronJob

from app.models.organization import Organization
from app.services import analytics_service
from app.workers import analytics as worker


class _NestedTransaction:
    """Minimal async context manager standing in for
    `AsyncSession.begin_nested()`'s SAVEPOINT context manager. `__aexit__`
    returns `False` (never suppresses an exception) — matching a real
    SAVEPOINT's rollback-then-reraise behavior."""

    async def __aenter__(self) -> "_NestedTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _SessionContextManager:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory(session: AsyncMock):
    def factory() -> _SessionContextManager:
        return _SessionContextManager(session)

    return factory


def _session_with_no_op_savepoints() -> AsyncMock:
    session = AsyncMock()
    session.begin_nested = MagicMock(side_effect=lambda: _NestedTransaction())
    return session


def _org(**overrides) -> Organization:
    defaults = dict(
        id=uuid4(),
        name="Test Org",
        chat_widget_key="widget-key",
        timezone="America/Argentina/Buenos_Aires",
        business_hours={},
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _patch_org_repo_list(monkeypatch, organizations: list[Organization]) -> None:
    from app.repositories.organization_repository import OrganizationRepository

    monkeypatch.setattr(
        OrganizationRepository, "list", AsyncMock(return_value=organizations)
    )


# ---------------------------------------------------------------------------
# rollup_daily_metrics — organization iteration + lookback window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_computes_last_two_completed_local_days_for_every_organization(
    monkeypatch,
) -> None:
    org1 = _org()
    org2 = _org()
    _patch_org_repo_list(monkeypatch, [org1, org2])

    fake_tickets = AsyncMock(return_value=[("tickets_created", Decimal(1))])
    fake_sla = AsyncMock(return_value=[("tickets_resolved", Decimal(0))])
    fake_chat = AsyncMock(return_value=[("chat_sessions_started", Decimal(0))])
    fake_classifier = AsyncMock(return_value=[])
    fake_rag = AsyncMock(return_value=[])
    fake_upsert = AsyncMock(return_value=None)
    monkeypatch.setattr(analytics_service, "tickets_family_metrics", fake_tickets)
    monkeypatch.setattr(analytics_service, "sla_family_metrics", fake_sla)
    monkeypatch.setattr(analytics_service, "chat_family_metrics", fake_chat)
    monkeypatch.setattr(analytics_service, "classifier_family_metrics", fake_classifier)
    monkeypatch.setattr(analytics_service, "rag_family_metrics", fake_rag)
    monkeypatch.setattr(analytics_service, "upsert_daily_metrics", fake_upsert)

    session = _session_with_no_op_savepoints()
    ctx = {"session_factory": _session_factory(session)}

    await worker.rollup_daily_metrics(ctx)

    # 2 orgs x 2 lookback days x 5 families = 20 calls total across all
    # family functions, i.e. each family function is called once per
    # (org, day) pair: 2 orgs x 2 days = 4.
    assert fake_tickets.await_count == 4
    assert fake_sla.await_count == 4
    assert fake_chat.await_count == 4
    assert fake_classifier.await_count == 4
    assert fake_rag.await_count == 4
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollup_lookback_window_is_the_last_two_COMPLETED_local_days_not_today(
    monkeypatch,
) -> None:
    """`lookback_days=2` (D3): recomputes YESTERDAY and the day before —
    never TODAY, which is not yet a completed org-local day."""
    org = _org(timezone="UTC")
    _patch_org_repo_list(monkeypatch, [org])

    fake_tickets = AsyncMock(return_value=[])
    fake_sla = AsyncMock(return_value=[])
    fake_chat = AsyncMock(return_value=[])
    fake_classifier = AsyncMock(return_value=[])
    fake_rag = AsyncMock(return_value=[])
    monkeypatch.setattr(analytics_service, "tickets_family_metrics", fake_tickets)
    monkeypatch.setattr(analytics_service, "sla_family_metrics", fake_sla)
    monkeypatch.setattr(analytics_service, "chat_family_metrics", fake_chat)
    monkeypatch.setattr(analytics_service, "classifier_family_metrics", fake_classifier)
    monkeypatch.setattr(analytics_service, "rag_family_metrics", fake_rag)
    monkeypatch.setattr(analytics_service, "upsert_daily_metrics", AsyncMock())

    session = _session_with_no_op_savepoints()
    ctx = {"session_factory": _session_factory(session)}

    fixed_now = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(worker, "_now", lambda: fixed_now)

    await worker.rollup_daily_metrics(ctx)

    days_queried = {call.args[2] for call in fake_tickets.call_args_list}
    assert days_queried == {date(2026, 8, 21), date(2026, 8, 20)}
    assert date(2026, 8, 22) not in days_queried  # today is never a completed day


# ---------------------------------------------------------------------------
# rollup_daily_metrics — per-family SAVEPOINT fault isolation (D2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_family_raising_does_not_prevent_the_other_families_from_committing(
    monkeypatch,
) -> None:
    org = _org()
    _patch_org_repo_list(monkeypatch, [org])

    fake_tickets = AsyncMock(side_effect=RuntimeError("tickets family exploded"))
    fake_sla = AsyncMock(return_value=[("tickets_resolved", Decimal(1))])
    fake_chat = AsyncMock(return_value=[("chat_sessions_started", Decimal(2))])
    fake_classifier = AsyncMock(return_value=[])
    fake_rag = AsyncMock(return_value=[])
    fake_upsert = AsyncMock(return_value=None)
    monkeypatch.setattr(analytics_service, "tickets_family_metrics", fake_tickets)
    monkeypatch.setattr(analytics_service, "sla_family_metrics", fake_sla)
    monkeypatch.setattr(analytics_service, "chat_family_metrics", fake_chat)
    monkeypatch.setattr(analytics_service, "classifier_family_metrics", fake_classifier)
    monkeypatch.setattr(analytics_service, "rag_family_metrics", fake_rag)
    monkeypatch.setattr(analytics_service, "upsert_daily_metrics", fake_upsert)

    session = _session_with_no_op_savepoints()
    ctx = {"session_factory": _session_factory(session)}

    # Must not raise — the tickets family's exception is caught and logged,
    # not propagated out of `rollup_daily_metrics`.
    await worker.rollup_daily_metrics(ctx)

    # tickets family was attempted for both lookback days but never upserted.
    assert fake_tickets.await_count == 2
    # sla + chat + classifier + rag families still ran, for both days.
    assert fake_sla.await_count == 2
    assert fake_chat.await_count == 2
    assert fake_classifier.await_count == 2
    assert fake_rag.await_count == 2
    upserted_keys = [
        row["metric_key"] for call in fake_upsert.call_args_list for row in call.args[1]
    ]
    assert "tickets_resolved" in upserted_keys
    assert "chat_sessions_started" in upserted_keys
    assert "tickets_created" not in upserted_keys

    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# rollup_daily_metrics — idempotency across two full runs (mirrors
# test_worker_sla.py's idempotency test shape)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_rollup_twice_for_the_same_day_upserts_stable_rows_no_duplicates(
    monkeypatch,
) -> None:
    org = _org()
    _patch_org_repo_list(monkeypatch, [org])

    monkeypatch.setattr(
        analytics_service,
        "tickets_family_metrics",
        AsyncMock(return_value=[("tickets_created", Decimal(5))]),
    )
    monkeypatch.setattr(
        analytics_service, "sla_family_metrics", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "chat_family_metrics", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "classifier_family_metrics", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        analytics_service, "rag_family_metrics", AsyncMock(return_value=[])
    )
    fake_upsert = AsyncMock(return_value=None)
    monkeypatch.setattr(analytics_service, "upsert_daily_metrics", fake_upsert)

    session = _session_with_no_op_savepoints()

    # --- First run. ---
    await worker.rollup_daily_metrics({"session_factory": _session_factory(session)})
    first_run_upsert_rows = [
        row for call in fake_upsert.call_args_list for row in call.args[1]
    ]

    fake_upsert.reset_mock()

    # --- Second run against the SAME (unchanged) computed data. ---
    await worker.rollup_daily_metrics({"session_factory": _session_factory(session)})
    second_run_upsert_rows = [
        row for call in fake_upsert.call_args_list for row in call.args[1]
    ]

    # Idempotency lives in `upsert_daily_metrics`'s `ON CONFLICT DO UPDATE`
    # (proven for real against Postgres by
    # `tests/integration/test_analytics_service.py`); at the worker level,
    # this test proves `rollup_daily_metrics` submits the EXACT SAME rows
    # both times rather than accumulating/duplicating state across runs.
    assert first_run_upsert_rows == second_run_upsert_rows
    assert len(first_run_upsert_rows) == len(second_run_upsert_rows) == 2  # 2 lookback days


# ---------------------------------------------------------------------------
# WorkerSettings.cron_jobs registration
# ---------------------------------------------------------------------------


def test_rollup_daily_metrics_is_registered_as_a_cron_job_at_00_30() -> None:
    from app.workers.settings import WorkerSettings

    matching = [
        job
        for job in WorkerSettings.cron_jobs
        if isinstance(job, CronJob) and job.coroutine.__name__ == "rollup_daily_metrics"
    ]
    assert len(matching) == 1, "expected exactly one rollup_daily_metrics cron job"
    assert matching[0].coroutine is worker.rollup_daily_metrics
    assert matching[0].hour == {0}
    assert matching[0].minute == {30}
