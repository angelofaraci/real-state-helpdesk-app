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
from app.models.enums import AuthorType, TicketChannel, UserRole
from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository
from app.services import message_service
from app.services.message_service import InvalidSuggestionReferenceError


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(), organization_id=scope.organization_id, channel=TicketChannel.WEB
    )
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


# ---------------------------------------------------------------------------
# 6.1/6.2 — `based_on_suggestion_id` validation on message creation
# ---------------------------------------------------------------------------


def _execute_scalar(scalar) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


@pytest.mark.asyncio
async def test_create_message_with_valid_suggestion_reference_persists_link(monkeypatch) -> None:
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    suggestion = _message(
        ticket.id, id=uuid4(), is_ai_suggestion=True, author_type=AuthorType.BOT
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_execute_scalar(suggestion))
    session.add = MagicMock()
    session.flush = AsyncMock()

    message = await message_service.create_message(
        session,
        scope=scope,
        ticket_id=ticket.id,
        content="Thanks, replaced the washer.",
        based_on_suggestion_id=suggestion.id,
    )

    assert message.based_on_suggestion_id == suggestion.id
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_create_message_rejects_suggestion_from_a_different_ticket(monkeypatch) -> None:
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    other_ticket_suggestion = _message(uuid4(), id=uuid4(), is_ai_suggestion=True)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_execute_scalar(other_ticket_suggestion))
    session.add = MagicMock()

    with pytest.raises(InvalidSuggestionReferenceError):
        await message_service.create_message(
            session,
            scope=scope,
            ticket_id=ticket.id,
            content="hi",
            based_on_suggestion_id=other_ticket_suggestion.id,
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_message_rejects_reference_to_a_non_suggestion_message(monkeypatch) -> None:
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    not_a_suggestion = _message(ticket.id, id=uuid4(), is_ai_suggestion=False)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_execute_scalar(not_a_suggestion))
    session.add = MagicMock()

    with pytest.raises(InvalidSuggestionReferenceError):
        await message_service.create_message(
            session,
            scope=scope,
            ticket_id=ticket.id,
            content="hi",
            based_on_suggestion_id=not_a_suggestion.id,
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_message_rejects_reference_to_a_nonexistent_message(monkeypatch) -> None:
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_execute_scalar(None))
    session.add = MagicMock()

    with pytest.raises(InvalidSuggestionReferenceError):
        await message_service.create_message(
            session,
            scope=scope,
            ticket_id=ticket.id,
            content="hi",
            based_on_suggestion_id=uuid4(),
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_message_rejects_self_reference(monkeypatch) -> None:
    """A message can never legitimately reference itself: the new
    message's own id is generated server-side (`UUIDPrimaryKeyMixin`'s
    Python-side `uuid.uuid4` default) and is never known to the client
    ahead of time. This test forces the degenerate collision by pinning
    `uuid.uuid4()` so the service's defense-in-depth check (mirroring the
    DB's own `ck_messages_based_on_suggestion_not_self` CHECK constraint)
    is proven to run and reject it with a clean 422-mappable error instead
    of ever reaching the database."""
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    pinned_id = uuid4()
    monkeypatch.setattr(message_service.uuid, "uuid4", lambda: pinned_id)
    session = AsyncMock()
    session.add = MagicMock()

    with pytest.raises(InvalidSuggestionReferenceError):
        await message_service.create_message(
            session,
            scope=scope,
            ticket_id=ticket.id,
            content="hi",
            based_on_suggestion_id=pinned_id,
        )

    session.add.assert_not_called()
    session.execute.assert_not_awaited()  # rejected before the lookup query


# ---------------------------------------------------------------------------
# 6.3/6.4 — Q1 visibility filter: AI suggestions hidden from tenant/owner,
# visible to agent/admin, in `list_messages`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
async def test_list_messages_filters_out_ai_suggestions_for_non_staff(monkeypatch, role) -> None:
    scope = _scope(role=role)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = _session_returning_scalars([])

    await message_service.list_messages(session, scope=scope, ticket_id=ticket.id)

    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    where_clause = sql.split("WHERE", 1)[1]
    assert "is_ai_suggestion" in where_clause
    assert "false" in where_clause.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.AGENT, UserRole.ADMIN])
async def test_list_messages_does_not_filter_ai_suggestions_for_staff(monkeypatch, role) -> None:
    scope = _scope(role=role)
    ticket = _ticket(scope)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = _session_returning_scalars([])

    await message_service.list_messages(session, scope=scope, ticket_id=ticket.id)

    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    where_clause = sql.split("WHERE", 1)[1]
    assert "is_ai_suggestion" not in where_clause


# ---------------------------------------------------------------------------
# Stage 5 (PR3) — `outbound_email_required`, the non-mapped flag `create_
# message` stashes on the returned `Message` for the API layer to check,
# mirroring `ticket_service.update_ticket`'s `entered_resolved_or_closed`
# idiom. Set ONLY when the parent ticket is `channel=email` AND the reply is
# staff-authored (`AuthorType.AGENT`) — a tenant/owner reply, or any reply on
# a non-email-channel ticket, never triggers an outbound send.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.AGENT, UserRole.ADMIN])
async def test_create_message_flags_outbound_email_required_for_staff_reply_on_email_ticket(
    monkeypatch, role
) -> None:
    scope = _scope(role=role)
    ticket = _ticket(scope, channel=TicketChannel.EMAIL)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    message = await message_service.create_message(
        session, scope=scope, ticket_id=ticket.id, content="We're sending someone over."
    )

    assert message.outbound_email_required is True


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.TENANT, UserRole.OWNER])
async def test_create_message_does_not_flag_outbound_email_for_non_staff_reply(
    monkeypatch, role
) -> None:
    scope = _scope(role=role)
    ticket = _ticket(scope, channel=TicketChannel.EMAIL)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    message = await message_service.create_message(
        session, scope=scope, ticket_id=ticket.id, content="Thanks for the update."
    )

    assert getattr(message, "outbound_email_required", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [TicketChannel.WEB, TicketChannel.WHATSAPP])
async def test_create_message_does_not_flag_outbound_email_for_staff_reply_on_non_email_ticket(
    monkeypatch, channel
) -> None:
    scope = _scope(role=UserRole.AGENT)
    ticket = _ticket(scope, channel=channel)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    message = await message_service.create_message(
        session, scope=scope, ticket_id=ticket.id, content="On it."
    )

    assert getattr(message, "outbound_email_required", False) is False
