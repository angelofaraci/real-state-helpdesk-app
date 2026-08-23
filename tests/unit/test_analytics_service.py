"""Unit tests for `app.services.analytics_service` — Stage 7 analytics
rollup query builders and upsert mechanics (PR3).

These tests exercise `local_day_utc_bounds`, each per-family query
builder's compiled SQL shape via a mocked `AsyncSession.execute` (mirroring
`tests/unit/test_sla_event_service.py`'s compiled-SQL + mocked-execute
pattern), and the family-aggregator functions' composition via monkeypatch
(mirroring `tests/unit/test_worker_sla.py`'s monkeypatch-the-collaborator
style). Real aggregation correctness against known-data fixtures — day
bucketing across different org timezones, the reopen/re-resolve
double-count, the escalated-ticket exclusion — is proven against a live
Postgres instance by `tests/integration/test_analytics_service_pg.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.organization import Organization
from app.services import analytics_service


def _org(**overrides) -> Organization:
    defaults = dict(
        id=uuid.uuid4(),
        name="Test Org",
        chat_widget_key="widget-key",
        timezone="America/Argentina/Buenos_Aires",
        business_hours={},
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _pg_compiled(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


# ---------------------------------------------------------------------------
# local_day_utc_bounds — org-local calendar day -> UTC datetime range (D3)
# ---------------------------------------------------------------------------


def test_local_day_utc_bounds_converts_local_midnight_to_utc() -> None:
    # Buenos Aires is UTC-3 year-round (no DST) — local midnight on
    # 2026-08-20 is 2026-08-20T03:00:00Z, and the next local midnight is
    # 2026-08-21T03:00:00Z.
    org = _org(timezone="America/Argentina/Buenos_Aires")

    start, end = analytics_service.local_day_utc_bounds(org, date(2026, 8, 20))

    assert start == datetime(2026, 8, 20, 3, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 21, 3, 0, 0, tzinfo=UTC)


def test_local_day_utc_bounds_differ_by_organization_timezone_for_the_same_calendar_day() -> None:
    """The whole point of D3: the SAME calendar `local_day` value maps to a
    DIFFERENT UTC range depending on the org's timezone — this is what lets
    two orgs bucket the same UTC instant into different `metric_date`
    rows."""
    org_ba = _org(timezone="America/Argentina/Buenos_Aires")  # UTC-3
    org_tokyo = _org(timezone="Asia/Tokyo")  # UTC+9

    ba_start, _ = analytics_service.local_day_utc_bounds(org_ba, date(2026, 8, 20))
    tokyo_start, _ = analytics_service.local_day_utc_bounds(org_tokyo, date(2026, 8, 20))

    assert ba_start != tokyo_start
    assert ba_start == datetime(2026, 8, 20, 3, 0, 0, tzinfo=UTC)
    assert tokyo_start == datetime(2026, 8, 19, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# tickets family — tickets_created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tickets_created_count_query_filters_by_org_and_local_day_utc_bounds() -> None:
    org = _org()
    session = AsyncMock()
    session.execute.return_value = _scalar_result(3)

    result = await analytics_service.tickets_created_count(session, org, date(2026, 8, 20))

    assert result == 3
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "FROM tickets" in sql
    assert "tickets.organization_id" in sql
    assert str(org.id) in sql
    assert "tickets.created_at >=" in sql
    assert "tickets.created_at <" in sql


@pytest.mark.asyncio
async def test_tickets_family_metrics_returns_tickets_created_only(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "tickets_created_count", AsyncMock(return_value=5)
    )

    metrics = await analytics_service.tickets_family_metrics(session, org, date(2026, 8, 20))

    assert metrics == [("tickets_created", Decimal(5))]


# ---------------------------------------------------------------------------
# SLA family — tickets_resolved, avg_resolution_hours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tickets_resolved_count_query_filters_sla_events_resolved_in_day() -> None:
    org = _org()
    session = AsyncMock()
    session.execute.return_value = _scalar_result(2)

    result = await analytics_service.tickets_resolved_count(session, org, date(2026, 8, 20))

    assert result == 2
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "FROM sla_events" in sql
    assert "'resolved'" in sql
    assert "sla_events.occurred_at >=" in sql


@pytest.mark.asyncio
async def test_avg_resolution_hours_query_excludes_tickets_with_an_escalated_chat_session() -> None:
    org = _org()
    session = AsyncMock()
    session.execute.return_value = _scalar_result(Decimal("4.5"))

    result = await analytics_service.avg_resolution_hours(session, org, date(2026, 8, 20))

    assert result == Decimal("4.5")
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "chat_sessions" in sql
    assert "escalated_at IS NOT NULL" in sql
    assert "NOT (EXISTS" in sql or "NOT EXISTS" in sql


@pytest.mark.asyncio
async def test_avg_resolution_hours_returns_none_when_no_resolved_events() -> None:
    org = _org()
    session = AsyncMock()
    session.execute.return_value = _scalar_result(None)

    result = await analytics_service.avg_resolution_hours(session, org, date(2026, 8, 20))

    assert result is None


@pytest.mark.asyncio
async def test_sla_family_metrics_omits_avg_resolution_hours_when_none(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "tickets_resolved_count", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        analytics_service, "avg_resolution_hours", AsyncMock(return_value=None)
    )

    metrics = await analytics_service.sla_family_metrics(session, org, date(2026, 8, 20))

    assert metrics == [("tickets_resolved", Decimal(0))]


@pytest.mark.asyncio
async def test_sla_family_metrics_includes_avg_resolution_hours_when_present(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "tickets_resolved_count", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        analytics_service, "avg_resolution_hours", AsyncMock(return_value=Decimal("3.25"))
    )

    metrics = await analytics_service.sla_family_metrics(session, org, date(2026, 8, 20))

    assert metrics == [
        ("tickets_resolved", Decimal(2)),
        ("avg_resolution_hours", Decimal("3.25")),
    ]


# ---------------------------------------------------------------------------
# chat family — chat_sessions_started, chat_escalation_rate, chat_to_ticket_rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_sessions_started_count_query_filters_by_org_and_local_day() -> None:
    org = _org()
    session = AsyncMock()
    session.execute.return_value = _scalar_result(10)

    result = await analytics_service.chat_sessions_started_count(session, org, date(2026, 8, 20))

    assert result == 10
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "FROM chat_sessions" in sql
    assert "chat_sessions.created_at >=" in sql


@pytest.mark.asyncio
async def test_chat_escalation_rate_divides_escalated_by_started_same_cohort_day(
    monkeypatch,
) -> None:
    """Both numerator and denominator key on the session START day (cohort
    semantics) — `escalated_at` need not fall on the same calendar day."""
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=10)
    )
    session.execute.return_value = _scalar_result(4)

    result = await analytics_service.chat_escalation_rate(session, org, date(2026, 8, 20))

    assert result == Decimal(4) / Decimal(10)
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "escalated_at IS NOT NULL" in sql
    assert "chat_sessions.created_at >=" in sql


@pytest.mark.asyncio
async def test_chat_escalation_rate_returns_none_when_no_sessions_started(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=0)
    )

    result = await analytics_service.chat_escalation_rate(session, org, date(2026, 8, 20))

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_to_ticket_rate_divides_sessions_with_ticket_by_started(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=10)
    )
    session.execute.return_value = _scalar_result(3)

    result = await analytics_service.chat_to_ticket_rate(session, org, date(2026, 8, 20))

    assert result == Decimal(3) / Decimal(10)
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "chat_sessions.ticket_id IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_chat_to_ticket_rate_returns_none_when_no_sessions_started(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=0)
    )

    result = await analytics_service.chat_to_ticket_rate(session, org, date(2026, 8, 20))

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_family_metrics_composes_all_three_chat_metrics(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=10)
    )
    monkeypatch.setattr(
        analytics_service, "chat_escalation_rate", AsyncMock(return_value=Decimal("0.4"))
    )
    monkeypatch.setattr(
        analytics_service, "chat_to_ticket_rate", AsyncMock(return_value=Decimal("0.3"))
    )

    metrics = await analytics_service.chat_family_metrics(session, org, date(2026, 8, 20))

    assert metrics == [
        ("chat_sessions_started", Decimal(10)),
        ("chat_escalation_rate", Decimal("0.4")),
        ("chat_to_ticket_rate", Decimal("0.3")),
    ]


@pytest.mark.asyncio
async def test_chat_family_metrics_omits_rates_when_zero_sessions_started(monkeypatch) -> None:
    org = _org()
    session = AsyncMock()
    monkeypatch.setattr(
        analytics_service, "chat_sessions_started_count", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        analytics_service, "chat_escalation_rate", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        analytics_service, "chat_to_ticket_rate", AsyncMock(return_value=None)
    )

    metrics = await analytics_service.chat_family_metrics(session, org, date(2026, 8, 20))

    assert metrics == [("chat_sessions_started", Decimal(0))]


# ---------------------------------------------------------------------------
# upsert_daily_metrics — ON CONFLICT DO UPDATE (D6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_daily_metrics_compiles_on_conflict_do_update() -> None:
    session = AsyncMock()
    rows = [
        {
            "organization_id": uuid.uuid4(),
            "metric_date": date(2026, 8, 20),
            "metric_key": "tickets_created",
            "metric_value": Decimal("3.0000"),
        }
    ]

    await analytics_service.upsert_daily_metrics(session, rows)

    session.execute.assert_awaited_once()
    (stmt,), _ = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "INSERT INTO daily_metrics" in sql
    assert "ON CONFLICT (organization_id, metric_date, metric_key)" in sql
    assert "DO UPDATE SET" in sql
    assert "metric_value = excluded.metric_value" in sql
    assert "updated_at = now()" in sql


@pytest.mark.asyncio
async def test_upsert_daily_metrics_assigns_a_fresh_id_per_call() -> None:
    """Multi-VALUES Core inserts bypass `UUIDPrimaryKeyMixin`'s Python-side
    ORM default (`default=uuid.uuid4` only applies to ORM-level inserts), so
    `upsert_daily_metrics` MUST assign an explicit `id` per row itself —
    proven here by two calls with identical row content compiling to
    DIFFERENT SQL (a fresh random `id` literal each time)."""
    session = AsyncMock()
    row = {
        "organization_id": uuid.uuid4(),
        "metric_date": date(2026, 8, 20),
        "metric_key": "tickets_created",
        "metric_value": Decimal("3.0000"),
    }

    await analytics_service.upsert_daily_metrics(session, [dict(row)])
    (stmt1,), _ = session.execute.call_args
    sql1 = _pg_compiled(stmt1)

    await analytics_service.upsert_daily_metrics(session, [dict(row)])
    (stmt2,), _ = session.execute.call_args
    sql2 = _pg_compiled(stmt2)

    assert sql1 != sql2


@pytest.mark.asyncio
async def test_upsert_daily_metrics_is_a_no_op_for_empty_rows() -> None:
    session = AsyncMock()

    await analytics_service.upsert_daily_metrics(session, [])

    session.execute.assert_not_awaited()
