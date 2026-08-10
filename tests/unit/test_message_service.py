"""Unit tests for `app.services.message_service` (Work Unit 8).
`AsyncSession` is mocked and `TicketRepository.get_or_404` is monkeypatched
directly, following the pattern established by `tests/unit/test_ticket_
service_update.py`.

The central behavior under test: both `list_messages` and `create_message`
must resolve the parent ticket through `TicketRepository.get_or_404` FIRST
— a caller who cannot see the ticket (wrong org, or same-org-but-role
-invisible, e.g. one tenant's ticket fetched by another tenant) gets the
exact same `NotFoundError` (404) on both operations, before any message
row is ever touched.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import AuthorType, UserRole
from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository
from app.services import message_service


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(id=uuid4(), organization_id=scope.organization_id)
    defaults.update(overrides)
    return Ticket(**defaults)


def _message(ticket_id, **overrides) -> Message:
    defaults = dict(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.USER,
        content="hello",
        is_ai_suggestion=False,
    )
    defaults.update(overrides)
    return Message(**defaults)


def _session_returning_scalars(scalars: list) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = scalars
    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.mark.asyncio
async def test_list_messages_raises_not_found_when_ticket_not_visible(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    monkeypatch.setattr(
        TicketRepository,
        "get_or_404",
        AsyncMock(side_effect=NotFoundError("Ticket", uuid4())),
    )
    session = AsyncMock()

    with pytest.raises(NotFoundError):
        await message_service.list_messages(session, scope=scope, ticket_id=uuid4())

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_message_raises_not_found_when_ticket_not_visible(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    monkeypatch.setattr(
        TicketRepository,
        "get_or_404",
        AsyncMock(side_effect=NotFoundError("Ticket", uuid4())),
    )
    session = AsyncMock()

    with pytest.raises(NotFoundError):
        await message_service.create_message(
            session, scope=scope, ticket_id=uuid4(), content="hello"
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_list_messages_returns_them_in_created_at_order(monkeypatch) -> None:
    scope = _scope()
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    first = _message(ticket.id, content="first")
    second = _message(ticket.id, content="second")
    session = _session_returning_scalars([first, second])

    result = await message_service.list_messages(session, scope=scope, ticket_id=ticket.id)

    assert result == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,expected",
    [
        (UserRole.AGENT, AuthorType.AGENT),
        (UserRole.ADMIN, AuthorType.AGENT),
        (UserRole.TENANT, AuthorType.USER),
        (UserRole.OWNER, AuthorType.USER),
    ],
)
async def test_create_message_derives_author_type_from_role(
    monkeypatch, role, expected
) -> None:
    scope = _scope(role=role)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    message = await message_service.create_message(
        session, scope=scope, ticket_id=ticket.id, content="hello there"
    )

    assert message.author_type == expected
    assert message.is_ai_suggestion is False
    assert message.content == "hello there"
    assert message.ticket_id == ticket.id
    session.add.assert_called_once()
