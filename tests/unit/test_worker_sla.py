"""Unit tests for `app.workers.sla` — the SLA monitor cron job
(`monitor_sla`, stage 6 — queues + SLA, PR6).

`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_worker_classification.py`: the worker opens/commits its
own session via an injected `ctx["session_factory"]`, and each per-ticket
warning/breach attempt runs inside `session.begin_nested()` (a SAVEPOINT),
so the fake session here also stubs `begin_nested()` as an async context
manager.

`record_once`/`fan_out` (both from stage 6 PR5) are monkeypatched on the
worker module rather than exercised against a real Postgres partial unique
index — the real index behavior is proven (skipped, no live Postgres in
this sandbox) by `tests/integration/test_sla_event_constraints.py`; these
tests instead prove `monitor_sla` calls those two functions with the right
arguments, in the right order, and reacts correctly to `record_once`'s
`None`-means-duplicate return contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from arq.cron import CronJob

from app.models.enums import NotificationKind, SlaEventType, TicketStatus
from app.models.ticket import Ticket
from app.workers import sla as worker


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
    """Minimal async context manager wrapping an already-built mock
    session — stands in for calling a real `async_sessionmaker` instance."""

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


def _execute_result(*, scalars_list: list | None = None) -> MagicMock:
    result = MagicMock()
    if scalars_list is not None:
        scalars = MagicMock()
        scalars.all.return_value = scalars_list
        result.scalars.return_value = scalars
    return result


def _session_with(scalars_list: list) -> AsyncMock:
    """A fake session whose single `execute()` call (the scan query)
    returns `scalars_list`, with a working `begin_nested()` SAVEPOINT
    context manager."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_execute_result(scalars_list=scalars_list))
    session.begin_nested = MagicMock(side_effect=lambda: _NestedTransaction())
    return session


def _ticket(
    *,
    organization_id,
    status: TicketStatus = TicketStatus.OPEN,
    created_at: datetime | None = None,
    sla_due_at: datetime | None = None,
    **overrides,
) -> Ticket:
    now = datetime.now(UTC)
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=None,
        urgency_id=None,
        title="Leaking sink",
        description="My sink is leaking badly",
        channel="web",
        status=status,
        classification_status="classified",
        agent_id=None,
        sla_due_at=sla_due_at,
        closed_at=None,
        created_at=created_at or (now - timedelta(hours=8)),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


# ---------------------------------------------------------------------------
# monitor_sla — scan query shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_query_excludes_terminal_statuses_and_null_sla_due_at() -> None:
    session = _session_with([])
    ctx = {"session_factory": _session_factory(session)}

    await worker.monitor_sla(ctx)

    (stmt,), _ = session.execute.call_args_list[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "sla_due_at IS NOT NULL" in sql
    assert "status NOT IN" in sql
    assert "'resolved'" in sql
    assert "'closed'" in sql
    # `escalated` must NOT be excluded — it is non-terminal and IS monitored.
    assert "'escalated'" not in sql


@pytest.mark.asyncio
async def test_scan_query_commits_and_is_a_no_op_with_zero_candidates() -> None:
    session = _session_with([])
    ctx = {"session_factory": _session_factory(session)}

    await worker.monitor_sla(ctx)

    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# monitor_sla — per-ticket warning/breach emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_past_80_percent_elapsed_records_warning_and_notifies(monkeypatch) -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    created_at = now - timedelta(hours=8)
    sla_due_at = now + timedelta(hours=2)  # 8h elapsed of a 10h window = 80%
    ticket = _ticket(
        organization_id=organization_id, created_at=created_at, sla_due_at=sla_due_at
    )

    session = _session_with([ticket])
    warning_event_id = uuid4()
    fake_record_once = AsyncMock(return_value=warning_event_id)
    fake_fan_out = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "record_once", fake_record_once)
    monkeypatch.setattr(worker, "fan_out", fake_fan_out)

    ctx = {"session_factory": _session_factory(session)}
    await worker.monitor_sla(ctx)

    fake_record_once.assert_awaited_once()
    _, kwargs = fake_record_once.call_args
    assert kwargs["ticket_id"] == ticket.id
    assert kwargs["event"] == SlaEventType.WARNING
    assert kwargs["scope"].organization_id == organization_id

    fake_fan_out.assert_awaited_once()
    _, kwargs = fake_fan_out.call_args
    assert kwargs["ticket"] is ticket
    assert kwargs["sla_event_id"] == warning_event_id
    assert kwargs["kind"] == NotificationKind.SLA_WARNING


@pytest.mark.asyncio
async def test_ticket_past_sla_due_at_records_both_warning_and_breach(monkeypatch) -> None:
    """A ticket already past 100% elapsed with NO prior `warning` event
    must get BOTH `warning` and `breached` recorded in the same pass —
    breach recording never requires warning to have fired first."""
    organization_id = uuid4()
    now = datetime.now(UTC)
    created_at = now - timedelta(hours=20)
    sla_due_at = now - timedelta(hours=1)  # already breached
    ticket = _ticket(
        organization_id=organization_id, created_at=created_at, sla_due_at=sla_due_at
    )

    session = _session_with([ticket])
    fake_record_once = AsyncMock(side_effect=[uuid4(), uuid4()])
    fake_fan_out = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "record_once", fake_record_once)
    monkeypatch.setattr(worker, "fan_out", fake_fan_out)

    ctx = {"session_factory": _session_factory(session)}
    await worker.monitor_sla(ctx)

    assert fake_record_once.await_count == 2
    events = [call.kwargs["event"] for call in fake_record_once.call_args_list]
    assert events == [SlaEventType.WARNING, SlaEventType.BREACHED]

    assert fake_fan_out.await_count == 2
    kinds = [call.kwargs["kind"] for call in fake_fan_out.call_args_list]
    assert kinds == [NotificationKind.SLA_WARNING, NotificationKind.SLA_BREACHED]


# ---------------------------------------------------------------------------
# monitor_sla — idempotency: this is the single most important proof in
# this PR. Running `monitor_sla` twice in a row against unchanged data must
# produce zero duplicate `sla_events` rows and zero duplicate
# notifications, purely because `record_once`'s `None` return (meaning
# "already recorded") short-circuits the `fan_out` call. `monitor_sla`
# itself carries NO "did I already process this" state of its own.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_monitor_sla_twice_produces_zero_duplicate_events_or_notifications(
    monkeypatch,
) -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    created_at = now - timedelta(hours=20)
    sla_due_at = now - timedelta(hours=1)  # already past both thresholds
    ticket = _ticket(
        organization_id=organization_id, created_at=created_at, sla_due_at=sla_due_at
    )

    fake_fan_out = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "fan_out", fake_fan_out)

    # --- First run: both events are genuinely new (fresh inserts). ---
    first_run_record_once = AsyncMock(side_effect=[uuid4(), uuid4()])
    monkeypatch.setattr(worker, "record_once", first_run_record_once)
    first_session = _session_with([ticket])
    await worker.monitor_sla({"session_factory": _session_factory(first_session)})

    assert first_run_record_once.await_count == 2
    assert fake_fan_out.await_count == 2

    # --- Second run against UNCHANGED data: `record_once`'s partial unique
    # index means both calls now return `None` (already recorded) — the
    # real contract this test proves `monitor_sla` respects. ---
    second_run_record_once = AsyncMock(side_effect=[None, None])
    monkeypatch.setattr(worker, "record_once", second_run_record_once)
    second_session = _session_with([ticket])
    await worker.monitor_sla({"session_factory": _session_factory(second_session)})

    # `record_once` is still called both times (idempotency lives in
    # `record_once`, not in `monitor_sla` skipping the attempt) ...
    assert second_run_record_once.await_count == 2
    # ... but because both returned `None`, `fan_out`'s total count across
    # BOTH runs stays at 2 — zero additional (duplicate) notifications from
    # the second, unchanged-data run.
    assert fake_fan_out.await_count == 2


# ---------------------------------------------------------------------------
# monitor_sla — one ticket's failure must never abort the whole pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_ticket_failing_does_not_prevent_a_later_healthy_ticket_from_processing(
    monkeypatch,
) -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    created_at = now - timedelta(hours=20)
    sla_due_at = now - timedelta(hours=1)

    failing_ticket = _ticket(
        organization_id=organization_id, created_at=created_at, sla_due_at=sla_due_at
    )
    healthy_ticket = _ticket(
        organization_id=organization_id, created_at=created_at, sla_due_at=sla_due_at
    )

    session = _session_with([failing_ticket, healthy_ticket])

    # `record_once` blows up entirely for the first ticket (both of its
    # calls would raise), then behaves normally for the second ticket.
    fake_record_once = AsyncMock(
        side_effect=[
            RuntimeError("db exploded for this ticket"),
            uuid4(),  # healthy_ticket warning
            uuid4(),  # healthy_ticket breach
        ]
    )
    fake_fan_out = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "record_once", fake_record_once)
    monkeypatch.setattr(worker, "fan_out", fake_fan_out)

    ctx = {"session_factory": _session_factory(session)}

    # Must not raise — the failing ticket's exception is caught and logged,
    # not propagated out of `monitor_sla`.
    await worker.monitor_sla(ctx)

    # 1 failed attempt (raised) + 2 successful attempts for the healthy
    # ticket = 3 total `record_once` calls.
    assert fake_record_once.await_count == 3
    # Only the healthy ticket's 2 successful `record_once` calls produced a
    # notification.
    assert fake_fan_out.await_count == 2
    for call in fake_fan_out.call_args_list:
        assert call.kwargs["ticket"] is healthy_ticket

    # The pass still commits despite the one ticket's failure.
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# WorkerSettings.cron_jobs registration
# ---------------------------------------------------------------------------


def test_monitor_sla_is_registered_as_a_cron_job_every_five_minutes() -> None:
    from app.workers.settings import WorkerSettings

    matching = [
        job
        for job in WorkerSettings.cron_jobs
        if isinstance(job, CronJob) and job.coroutine.__name__ == "monitor_sla"
    ]
    assert len(matching) == 1, "expected exactly one monitor_sla cron job"
    assert matching[0].coroutine is worker.monitor_sla
    assert matching[0].minute == set(range(0, 60, 5))
