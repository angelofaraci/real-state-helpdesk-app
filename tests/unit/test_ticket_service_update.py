"""Unit tests for `app.services.ticket_service.update_ticket` (Work Unit
7b, Rule B — manual-only agent assignment + tenant/owner status
restriction). `AsyncSession` is mocked, following the pattern established
by `tests/unit/test_property_service.py`.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import SlaEventType, TicketStatus, UserRole
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.models.user import User
from app.services import ticket_service
from app.services.ticket_service import ForbiddenError, InvalidAgentError


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        category_id=uuid4(),
        urgency_id=uuid4(),
        channel="web",
        status=TicketStatus.OPEN,
        agent_id=None,
        sla_due_at=datetime.now(UTC),
        closed_at=None,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _user(scope: OrgScope, **overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Some Agent",
        email="agent@example.com",
        role=UserRole.AGENT,
        status="active",
    )
    defaults.update(overrides)
    return User(**defaults)


def _urgency(scope: OrgScope, **overrides) -> UrgencyLevel:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="High",
        sla_hours=4,
        sort_order=0,
        active=True,
    )
    defaults.update(overrides)
    return UrgencyLevel(**defaults)


def _session_returning_sequence(*scalars: object) -> AsyncMock:
    results = []
    for scalar in scalars:
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar
        results.append(execute_result)
    session = AsyncMock()
    session.execute.side_effect = results
    return session


@pytest.mark.asyncio
async def test_agent_assignment_by_non_staff_role_is_forbidden_before_any_lookup() -> None:
    scope = _scope(role=UserRole.TENANT)
    session = _session_returning_sequence()

    with pytest.raises(ForbiddenError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), agent_id=uuid4()
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolving_by_non_staff_role_is_forbidden_before_any_lookup() -> None:
    scope = _scope(role=UserRole.OWNER)
    session = _session_returning_sequence()

    with pytest.raises(ForbiddenError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), status=TicketStatus.RESOLVED
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_by_non_staff_role_is_forbidden_before_any_lookup() -> None:
    scope = _scope(role=UserRole.TENANT)
    session = _session_returning_sequence()

    with pytest.raises(ForbiddenError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), status=TicketStatus.CLOSED
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_may_set_a_non_terminal_status() -> None:
    scope = _scope(role=UserRole.TENANT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.WAITING_ON_CUSTOMER
    )

    assert updated.status == TicketStatus.WAITING_ON_CUSTOMER
    assert updated.closed_at is None


@pytest.mark.asyncio
async def test_ticket_not_visible_to_scope_raises_not_found() -> None:
    scope = _scope(role=UserRole.AGENT)
    session = _session_returning_sequence(None)

    with pytest.raises(NotFoundError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), status=TicketStatus.IN_PROGRESS
        )


@pytest.mark.asyncio
async def test_agent_id_referencing_a_non_agent_role_raises_invalid_agent_error() -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(scope)
    not_an_agent = _user(scope, role=UserRole.TENANT)
    session = _session_returning_sequence(target, not_an_agent)

    with pytest.raises(InvalidAgentError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=target.id, agent_id=not_an_agent.id
        )


@pytest.mark.asyncio
async def test_agent_id_not_resolving_in_scope_raises_not_found() -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(scope)
    session = _session_returning_sequence(target, None)

    with pytest.raises(NotFoundError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=target.id, agent_id=uuid4()
        )


@pytest.mark.asyncio
async def test_assigning_a_valid_agent_succeeds() -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(scope)
    agent = _user(scope, role=UserRole.AGENT)
    session = _session_returning_sequence(target, agent, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, agent_id=agent.id
    )

    assert updated.agent_id == agent.id


@pytest.mark.asyncio
async def test_setting_status_closed_sets_closed_at() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN, closed_at=None)
    session = _session_returning_sequence(target, target)

    before = datetime.now(UTC)
    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.CLOSED
    )

    assert updated.status == TicketStatus.CLOSED
    assert updated.closed_at is not None
    assert updated.closed_at >= before


@pytest.mark.asyncio
async def test_setting_a_non_closed_status_clears_closed_at() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.CLOSED, closed_at=datetime.now(UTC))
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.IN_PROGRESS
    )

    assert updated.status == TicketStatus.IN_PROGRESS
    assert updated.closed_at is None


# ---------------------------------------------------------------------------
# Stage 2 — recategorization (PR5): an agent/admin correcting `category_id`/
# `urgency_id` flags the existing `classifications` row as human-corrected
# and recomputes `sla_due_at` anchored on `ticket.created_at` (matching the
# worker's SLA-anchor convention), NOT `datetime.now()`. A tenant/owner may
# never set these fields via update.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recategorizing_by_non_staff_role_is_forbidden_before_any_lookup() -> None:
    scope = _scope(role=UserRole.TENANT)
    session = _session_returning_sequence()

    with pytest.raises(ForbiddenError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), category_id=uuid4()
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_recategorizing_urgency_only_by_owner_is_forbidden_before_any_lookup() -> None:
    scope = _scope(role=UserRole.OWNER)
    session = _session_returning_sequence()

    with pytest.raises(ForbiddenError):
        await ticket_service.update_ticket(
            session, scope=scope, ticket_id=uuid4(), urgency_id=uuid4()
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_updating_category_and_urgency_marks_human_corrected_and_recomputes_sla(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.AGENT)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    target = _ticket(scope, created_at=created_at)
    new_urgency = _urgency(scope, sla_hours=12)
    new_category_id = uuid4()
    session = _session_returning_sequence(target, new_urgency, None, target)
    # PR4: `sla_due_at` now goes through `resolve_sla_due_at` (which would
    # issue its own `OrganizationRepository` lookup for real) — mocked here
    # so this test's session sequence/index assertions below stay unchanged;
    # the wiring itself is covered by
    # `test_manual_correction_recomputes_sla_via_resolve_sla_due_at`.
    monkeypatch.setattr(
        ticket_service,
        "resolve_sla_due_at",
        AsyncMock(return_value=created_at + timedelta(hours=12)),
    )

    updated = await ticket_service.update_ticket(
        session,
        scope=scope,
        ticket_id=target.id,
        category_id=new_category_id,
        urgency_id=new_urgency.id,
    )

    assert updated.category_id == new_category_id
    assert updated.urgency_id == new_urgency.id
    assert updated.sla_due_at == created_at + timedelta(hours=12)

    # The 3rd execute call is `ClassificationRepository.mark_human_corrected`.
    mark_call = session.execute.call_args_list[2]
    (stmt,), _ = mark_call
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE classifications" in sql
    assert "human_corrected=true" in sql or "human_corrected = true" in sql
    assert target.id.hex in sql


# ---------------------------------------------------------------------------
# Stage 3 (PR5) — `entered_resolved_or_closed`: a plain non-mapped attribute
# `update_ticket` stashes on the returned `Ticket` instance (same pattern as
# `get_ticket` attaching `predicted_category` etc.), signalling the API
# route layer whether THIS PATCH transitioned the ticket's status from a
# non-terminal value into `RESOLVED`/`CLOSED`, so it knows whether to
# enqueue `embed_resolved_ticket`. Computed from the OLD status (captured
# before `repo.update()` mutates the ORM instance in place) vs. the NEW
# status — never from `status is not None` alone, since a re-PATCH of an
# already-resolved/closed ticket (e.g. an unrelated `agent_id` change, or
# re-sending the same status) must NOT re-trigger the embed job.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_into_resolved_flags_entered_resolved_or_closed() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.RESOLVED
    )

    assert updated.entered_resolved_or_closed is True


@pytest.mark.asyncio
async def test_transition_into_closed_flags_entered_resolved_or_closed() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.CLOSED
    )

    assert updated.entered_resolved_or_closed is True


@pytest.mark.asyncio
async def test_transition_from_resolved_to_closed_still_flags_it() -> None:
    """A ticket moving RESOLVED -> CLOSED is a distinct entry into CLOSED
    (a real status change), so it still flags -- the worker-side `upsert`
    (not this flag) is what makes a resulting double-fire idempotent."""
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.RESOLVED)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.CLOSED
    )

    assert updated.entered_resolved_or_closed is True


@pytest.mark.asyncio
async def test_unrelated_patch_on_an_already_resolved_ticket_does_not_flag() -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(scope, status=TicketStatus.RESOLVED)
    agent = _user(scope, role=UserRole.AGENT)
    session = _session_returning_sequence(target, agent, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, agent_id=agent.id
    )

    assert updated.entered_resolved_or_closed is False


@pytest.mark.asyncio
async def test_resending_the_same_terminal_status_does_not_flag() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.CLOSED)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.CLOSED
    )

    assert updated.entered_resolved_or_closed is False


@pytest.mark.asyncio
async def test_reopen_does_not_flag() -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.CLOSED)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.OPEN
    )

    assert updated.entered_resolved_or_closed is False


@pytest.mark.asyncio
async def test_reopen_then_reresolve_flags_again() -> None:
    """Reopen -> re-resolve: a fresh transition into RESOLVED from a
    non-terminal status, so it must flag again (the worker regenerates the
    summary)."""
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.RESOLVED
    )

    assert updated.entered_resolved_or_closed is True


@pytest.mark.asyncio
async def test_no_status_in_patch_does_not_flag(monkeypatch) -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(
        scope, status=TicketStatus.OPEN, urgency_id=uuid4(), created_at=datetime.now(UTC)
    )
    existing_urgency = _urgency(scope, id=target.urgency_id, sla_hours=2)
    new_category_id = uuid4()
    session = _session_returning_sequence(target, existing_urgency, None, target)
    monkeypatch.setattr(
        ticket_service, "resolve_sla_due_at", AsyncMock(return_value=datetime.now(UTC))
    )

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, category_id=new_category_id
    )

    assert updated.entered_resolved_or_closed is False


@pytest.mark.asyncio
async def test_updating_category_only_reuses_the_tickets_existing_urgency_for_sla(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.ADMIN)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    existing_urgency_id = uuid4()
    target = _ticket(scope, created_at=created_at, urgency_id=existing_urgency_id)
    existing_urgency = _urgency(scope, id=existing_urgency_id, sla_hours=2)
    new_category_id = uuid4()
    session = _session_returning_sequence(target, existing_urgency, None, target)
    monkeypatch.setattr(
        ticket_service,
        "resolve_sla_due_at",
        AsyncMock(return_value=created_at + timedelta(hours=2)),
    )

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, category_id=new_category_id
    )

    assert updated.category_id == new_category_id
    assert updated.urgency_id == existing_urgency_id
    assert updated.sla_due_at == created_at + timedelta(hours=2)


# ---------------------------------------------------------------------------
# Stage 6 (PR4) — writer rewiring: manual recategorization must recompute
# `sla_due_at` via `resolve_sla_due_at` (not an inline
# `created_at + timedelta(...)`), anchored on `ticket.created_at` — unifying
# it onto the same code path `create_ticket`/`classify_ticket` now use.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_correction_recomputes_sla_via_resolve_sla_due_at(monkeypatch) -> None:
    scope = _scope(role=UserRole.AGENT)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    target = _ticket(scope, created_at=created_at)
    new_urgency = _urgency(scope, sla_hours=12)
    new_category_id = uuid4()
    session = _session_returning_sequence(target, new_urgency, None, target)

    fake_due_at = datetime(2030, 5, 5, tzinfo=UTC)
    fake_resolve = AsyncMock(return_value=fake_due_at)
    monkeypatch.setattr(ticket_service, "resolve_sla_due_at", fake_resolve)

    updated = await ticket_service.update_ticket(
        session,
        scope=scope,
        ticket_id=target.id,
        category_id=new_category_id,
        urgency_id=new_urgency.id,
    )

    fake_resolve.assert_awaited_once()
    _, kwargs = fake_resolve.call_args
    assert kwargs["from_timestamp"] == created_at
    assert kwargs["urgency"] is new_urgency
    assert kwargs["organization_id"] == scope.organization_id
    assert updated.sla_due_at == fake_due_at


# ---------------------------------------------------------------------------
# Stage 6 (PR5) — `record_resolved` hook: `update_ticket` synchronously
# records a `resolved` `sla_events` row (same transaction as the PATCH, no
# `BackgroundTasks`) at the exact point `entered_resolved_or_closed` fires.
# Per the brief, `resolved` events never trigger a notification (only
# `warning`/`breached` do) — `update_ticket` never touches
# `notification_service` at all, so that's proven by its simple absence
# from every call in this section.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_into_resolved_synchronously_records_a_resolved_sla_event(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)
    fake_record_resolved = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(
        ticket_service.sla_event_service, "record_resolved", fake_record_resolved
    )

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.RESOLVED
    )

    assert updated.entered_resolved_or_closed is True
    fake_record_resolved.assert_awaited_once()
    _, kwargs = fake_record_resolved.call_args
    assert kwargs["scope"] is scope
    assert kwargs["ticket_id"] == target.id
    assert kwargs["sla_due_at"] == updated.sla_due_at


@pytest.mark.asyncio
async def test_transition_into_closed_synchronously_records_a_resolved_sla_event(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target)
    fake_record_resolved = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(
        ticket_service.sla_event_service, "record_resolved", fake_record_resolved
    )

    await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.CLOSED
    )

    fake_record_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_patch_on_an_already_resolved_ticket_does_not_record_an_sla_event(
    monkeypatch,
) -> None:
    scope = _scope(role=UserRole.ADMIN)
    target = _ticket(scope, status=TicketStatus.RESOLVED)
    agent = _user(scope, role=UserRole.AGENT)
    session = _session_returning_sequence(target, agent, target)
    fake_record_resolved = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(
        ticket_service.sla_event_service, "record_resolved", fake_record_resolved
    )

    updated = await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, agent_id=agent.id
    )

    assert updated.entered_resolved_or_closed is False
    fake_record_resolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_then_reresolve_records_two_distinct_resolved_sla_events() -> None:
    """End-to-end proof (through the real, non-mocked
    `sla_event_service.record_resolved` — NOT monkeypatched here) that
    decision #5's "`resolved` may repeat" semantics actually hold through
    `update_ticket`'s hook: resolve -> reopen -> re-resolve records TWO
    distinct `resolved` `sla_events` inserts, never short-circuited by any
    conflict handling (matching `test_sla_event_service`'s isolated proof
    of the same behavior, but exercised through the real service call
    chain this time)."""
    scope = _scope(role=UserRole.AGENT)
    target = _ticket(scope, status=TicketStatus.OPEN)
    session = _session_returning_sequence(target, target, target, target, target, target)

    await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.RESOLVED
    )
    await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.OPEN
    )
    await ticket_service.update_ticket(
        session, scope=scope, ticket_id=target.id, status=TicketStatus.RESOLVED
    )

    assert session.add.call_count == 2
    first_event = session.add.call_args_list[0].args[0]
    second_event = session.add.call_args_list[1].args[0]
    assert first_event is not second_event
    assert first_event.event == SlaEventType.RESOLVED
    assert second_event.event == SlaEventType.RESOLVED
    assert first_event.ticket_id == target.id
    assert second_event.ticket_id == target.id
    assert session.flush.await_count == 2
