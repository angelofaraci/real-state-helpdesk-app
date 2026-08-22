"""Unit tests for `app.services.sla_event_service` — `record_once`/
`record_resolved`, the safety-critical write path behind Stage 6's SLA
warning/breach idempotency guard (`ux_sla_events_ticket_event_once`,
migration 0009) and the always-inserts `resolved` audit trail.

No live Postgres is available in this sandbox (see
`tests/integration/test_sla_event_constraints.py`), so `record_once`'s
`ON CONFLICT ... DO NOTHING` semantics are verified two ways, mirroring
`tests/unit/test_classification_repository.py`'s compiled-SQL pattern:

1. The compiled SQL asserts the `index_elements`/`index_where` clause
   matches migration 0009's `ux_sla_events_ticket_event_once` partial
   index predicate BYTE FOR BYTE (`event IN ('warning', 'breached')`) —
   a mismatch here means Postgres cannot infer the conflict target and
   the INSERT raises instead of silently no-op'ing.
2. A mocked `AsyncSession.execute` simulates Postgres's actual behavior
   (`RETURNING id` yields a real id on the first call, `None` on a
   simulated conflict) to prove the function's return-value contract.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.core.scope import OrgScope
from app.models.enums import SlaEventType, UserRole
from app.services import sla_event_service


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _pg_compiled(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _execute_result(scalar: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


# ---------------------------------------------------------------------------
# record_once: INSERT ... ON CONFLICT (ticket_id, event) WHERE event IN
# ('warning', 'breached') DO NOTHING RETURNING id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_once_compiles_on_conflict_do_nothing_matching_the_partial_index() -> None:
    """The single most safety-critical assertion in this PR: `index_where`
    must match migration 0009's raw-SQL predicate EXACTLY, or Postgres
    cannot infer the conflict target from `index_elements` alone."""
    scope = _scope()
    session = AsyncMock()
    session.execute.return_value = _execute_result(uuid.uuid4())
    ticket_id = uuid.uuid4()

    await sla_event_service.record_once(
        session, scope=scope, ticket_id=ticket_id, event=SlaEventType.WARNING
    )

    session.execute.assert_awaited_once()
    (stmt,), _kwargs = session.execute.call_args
    sql = _pg_compiled(stmt)

    assert "INSERT INTO sla_events" in sql
    assert "ON CONFLICT (ticket_id, event)" in sql
    assert "WHERE event IN ('warning', 'breached')" in sql
    assert "DO NOTHING" in sql
    assert "RETURNING sla_events.id" in sql
    assert str(ticket_id) in sql
    assert str(scope.organization_id) in sql
    assert "'warning'" in sql


@pytest.mark.asyncio
async def test_record_once_returns_the_new_id_on_first_insert() -> None:
    scope = _scope()
    session = AsyncMock()
    new_id = uuid.uuid4()
    session.execute.return_value = _execute_result(new_id)

    result = await sla_event_service.record_once(
        session, scope=scope, ticket_id=uuid.uuid4(), event=SlaEventType.WARNING
    )

    assert result == new_id


@pytest.mark.asyncio
async def test_record_once_returns_none_when_a_row_already_exists() -> None:
    """Simulates Postgres's `DO NOTHING` no-op: `RETURNING` yields no row,
    so `scalar_one_or_none()` is `None` — the caller's signal to skip
    notification fan-out for an already-recorded event."""
    scope = _scope()
    session = AsyncMock()
    session.execute.return_value = _execute_result(None)

    result = await sla_event_service.record_once(
        session, scope=scope, ticket_id=uuid.uuid4(), event=SlaEventType.WARNING
    )

    assert result is None


@pytest.mark.asyncio
async def test_record_once_for_breached_after_warning_is_an_independent_conflict_target() -> None:
    """`warning` and `breached` are different `event` values, so the
    partial unique index's `(ticket_id, event)` composite key treats them
    as independent conflict targets — recording `breached` for a ticket
    that already has a `warning` row must succeed, not be treated as a
    conflict."""
    scope = _scope()
    session = AsyncMock()
    ticket_id = uuid.uuid4()
    breached_id = uuid.uuid4()
    session.execute.return_value = _execute_result(breached_id)

    result = await sla_event_service.record_once(
        session, scope=scope, ticket_id=ticket_id, event=SlaEventType.BREACHED
    )

    assert result == breached_id
    (stmt,), _kwargs = session.execute.call_args
    sql = _pg_compiled(stmt)
    assert "'breached'" in sql


# ---------------------------------------------------------------------------
# record_resolved: plain insert, no conflict handling — `resolved` may
# legitimately repeat across a ticket's reopen/re-resolve cycles.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_resolved_inserts_without_conflict_handling() -> None:
    scope = _scope()
    session = AsyncMock()
    ticket_id = uuid.uuid4()

    result = await sla_event_service.record_resolved(session, scope=scope, ticket_id=ticket_id)

    session.add.assert_called_once()
    (instance,), _kwargs = session.add.call_args
    assert instance.ticket_id == ticket_id
    assert instance.event == SlaEventType.RESOLVED
    assert instance.organization_id == scope.organization_id
    session.flush.assert_awaited_once()
    assert result == instance.id


@pytest.mark.asyncio
async def test_record_resolved_called_twice_creates_two_distinct_rows() -> None:
    """`resolved` is deliberately excluded from
    `ux_sla_events_ticket_event_once` — a ticket's reopen/re-resolve cycle
    must be able to record it more than once. With a mocked session no
    real DB-side id is generated, so "two distinct rows" is proven by two
    separate `SlaEvent` instances each independently passed to
    `session.add`/`session.flush` (no fetch-then-branch, no conflict
    short-circuit) rather than by comparing generated ids."""
    scope = _scope()
    session = AsyncMock()
    ticket_id = uuid.uuid4()

    await sla_event_service.record_resolved(session, scope=scope, ticket_id=ticket_id)
    await sla_event_service.record_resolved(session, scope=scope, ticket_id=ticket_id)

    assert session.add.call_count == 2
    first_instance = session.add.call_args_list[0].args[0]
    second_instance = session.add.call_args_list[1].args[0]
    assert first_instance is not second_instance
    assert first_instance.ticket_id == ticket_id
    assert second_instance.ticket_id == ticket_id
    assert first_instance.event == SlaEventType.RESOLVED
    assert second_instance.event == SlaEventType.RESOLVED
    assert session.flush.await_count == 2
