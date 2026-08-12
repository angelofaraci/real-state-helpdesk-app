"""Unit tests for `app.services.ticket_service` (`list_tickets`,
`get_ticket`). `AsyncSession` is mocked, following the pattern established by
`tests/unit/test_property_service.py`. Creation/update land in Work Unit 7b.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.classification import Classification
from app.models.enums import TicketChannel, TicketStatus, UserRole
from app.models.ticket import Ticket
from app.services import ticket_service


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
        channel=TicketChannel.WEB,
        status=TicketStatus.OPEN,
        agent_id=None,
        sla_due_at=datetime.now(UTC) + timedelta(hours=4),
        closed_at=None,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


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
async def test_list_tickets_returns_scalars_from_the_repository_query() -> None:
    scope = _scope()
    tickets = [_ticket(scope), _ticket(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = tickets
    session = AsyncMock()
    session.execute.return_value = execute_result

    assert await ticket_service.list_tickets(session, scope=scope) == tickets


@pytest.mark.asyncio
async def test_list_tickets_forwards_filters_to_the_repository() -> None:
    scope = _scope()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.return_value = execute_result

    result = await ticket_service.list_tickets(
        session, scope=scope, status=TicketStatus.OPEN, category_id=uuid4()
    )

    assert result == []
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_ticket_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _ticket(scope)
    # 2nd execute call: `ClassificationRepository.get_by_ticket_id` — no
    # classification row exists for this ticket.
    session = _session_returning_sequence(target, None)

    result = await ticket_service.get_ticket(session, scope=scope, ticket_id=target.id)

    assert result is target
    assert result.predicted_category is None
    assert result.confidence is None
    assert result.model_used is None


@pytest.mark.asyncio
async def test_get_ticket_raises_not_found_when_out_of_scope_or_invisible() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await ticket_service.get_ticket(session, scope=_scope(), ticket_id=uuid4())


@pytest.mark.asyncio
async def test_get_ticket_attaches_classification_metadata_when_a_row_exists() -> None:
    """Stage 2 (PR5): when the ticket has been classified (worker or
    human), `get_ticket` attaches the predicted category/urgency,
    confidence, and `model_used` from its `classifications` row onto the
    returned ticket — the role-gating of these fields happens at the API
    router, not here."""
    scope = _scope()
    target = _ticket(scope)
    classification = Classification(
        ticket_id=target.id,
        predicted_category="Plumbing",
        confidence=0.91,
        predicted_urgency="High",
        urgency_confidence=0.82,
        model_used="sklearn_v1",
    )
    session = _session_returning_sequence(target, classification)

    result = await ticket_service.get_ticket(session, scope=scope, ticket_id=target.id)

    assert result.predicted_category == "Plumbing"
    assert result.confidence == 0.91
    assert result.predicted_urgency == "High"
    assert result.urgency_confidence == 0.82
    assert result.model_used == "sklearn_v1"
