"""Unit tests for `app.services.ticket_service.update_ticket` (Work Unit
7b, Rule B — manual-only agent assignment + tenant/owner status
restriction). `AsyncSession` is mocked, following the pattern established
by `tests/unit/test_property_service.py`.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import TicketStatus, UserRole
from app.models.ticket import Ticket
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
