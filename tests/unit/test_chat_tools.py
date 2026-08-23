"""Unit tests for `app.services.chat_tools` (stage 4 — chatbot, PR3).

`AsyncSession` is mocked and the relevant repository methods /
`ticket_service.create_ticket` are monkeypatched directly, following the
pattern established by `tests/unit/test_message_service.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.chat_session import ChatSession
from app.models.contract import Contract
from app.models.enums import (
    ChatSessionStatus,
    ContractStatus,
    TicketChannel,
    TicketStatus,
    UserRole,
)
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository
from app.services import chat_tools, ticket_service
from app.services.chat_tools import ChatToolContext


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.TENANT)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _chat_session(scope: OrgScope, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
        ticket_id=None,
        status=ChatSessionStatus.ACTIVE,
        low_confidence_streak=0,
        last_activity_at=datetime.now(UTC),
        # Stage 5 — multichannel: real rows always have a channel (NOT
        # NULL, migration 0007), so tests that don't care about it default
        # to WEB, same as the DB's own server_default.
        channel=TicketChannel.WEB,
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _contract(scope: OrgScope, **overrides) -> Contract:
    defaults = dict(
        id=uuid4(),
        property_id=uuid4(),
        tenant_id=scope.user_id,
        status=ContractStatus.ACTIVE,
    )
    defaults.update(overrides)
    return Contract(**defaults)


def _ticket(scope: OrgScope, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
        title="Chat escalation",
        status=TicketStatus.OPEN,
        category_id=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


# ---------------------------------------------------------------------------
# 3.1 — check_ticket_status: cross-user and nonexistent must be identical.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ticket_status_cross_user_returns_not_found(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    ctx = ChatToolContext(session=AsyncMock(), scope=scope, chat_session=_chat_session(scope))
    monkeypatch.setattr(
        TicketRepository,
        "get_or_404",
        AsyncMock(side_effect=NotFoundError("Ticket", uuid4())),
    )

    cross_user_result = await chat_tools.check_ticket_status(ctx, ticket_id=uuid4())
    nonexistent_result = await chat_tools.check_ticket_status(ctx, ticket_id=uuid4())

    assert cross_user_result == {"found": False}
    assert nonexistent_result == {"found": False}
    assert cross_user_result == nonexistent_result


@pytest.mark.asyncio
async def test_check_ticket_status_visible_ticket_returns_details(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    ctx = ChatToolContext(session=AsyncMock(), scope=scope, chat_session=_chat_session(scope))
    ticket = _ticket(scope, status=TicketStatus.IN_PROGRESS)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))

    result = await chat_tools.check_ticket_status(ctx, ticket_id=ticket.id)

    assert result["found"] is True
    assert result["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 3.2 — create_ticket: ambiguous ACTIVE contract count needs clarification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_count", [0, 2])
async def test_create_ticket_ambiguous_contract_needs_clarification(
    monkeypatch, contract_count
) -> None:
    scope = _scope(role=UserRole.TENANT)
    ctx = ChatToolContext(session=AsyncMock(), scope=scope, chat_session=_chat_session(scope))
    contracts = [_contract(scope) for _ in range(contract_count)]
    monkeypatch.setattr(
        chat_tools, "_active_contracts_for_caller", AsyncMock(return_value=contracts)
    )
    create_ticket_mock = AsyncMock()
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)

    result = await chat_tools.create_ticket(ctx, title="Leak", description="Bathroom leak")

    assert result["needs_clarification"] is True
    assert len(result["options"]) == contract_count
    create_ticket_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_ticket_single_contract_creates_and_copies_messages(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    contract = _contract(scope)
    monkeypatch.setattr(
        chat_tools, "_active_contracts_for_caller", AsyncMock(return_value=[contract])
    )
    ticket = _ticket(scope)
    create_ticket_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    monkeypatch.setattr(chat_tools, "_copy_chat_messages_to_ticket", AsyncMock())

    result = await chat_tools.create_ticket(ctx, title="Leak", description="Bathroom leak")

    create_ticket_mock.assert_awaited_once()
    _, kwargs = create_ticket_mock.call_args
    assert kwargs["property_id"] == contract.property_id
    assert kwargs["contract_id"] == contract.id
    assert kwargs["category_id"] is None
    assert chat_session.ticket_id == ticket.id
    assert result == {"ticket_id": str(ticket.id), "status": ticket.status.value}


# ---------------------------------------------------------------------------
# 3.2b — stage 5 multichannel: created tickets are attributed to the actual
# chat session's channel, never hardcoded to WEB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel", [TicketChannel.WEB, TicketChannel.EMAIL, TicketChannel.WHATSAPP]
)
async def test_create_ticket_uses_the_chat_sessions_own_channel(monkeypatch, channel) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope, channel=channel)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    contract = _contract(scope)
    monkeypatch.setattr(
        chat_tools, "_active_contracts_for_caller", AsyncMock(return_value=[contract])
    )
    ticket = _ticket(scope)
    create_ticket_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    monkeypatch.setattr(chat_tools, "_copy_chat_messages_to_ticket", AsyncMock())

    await chat_tools.create_ticket(ctx, title="Leak", description="Bathroom leak")

    _, kwargs = create_ticket_mock.call_args
    assert kwargs["channel"] == channel


# ---------------------------------------------------------------------------
# 3.3 — schedule_visit requires a linked ticket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_visit_requires_linked_ticket(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope, ticket_id=None)
    session = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    get_or_404_mock = AsyncMock()
    monkeypatch.setattr(TicketRepository, "get_or_404", get_or_404_mock)

    result = await chat_tools.schedule_visit(
        ctx, ticket_id=uuid4(), preferred_date="2026-09-01"
    )

    assert "error" in result
    get_or_404_mock.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_visit_with_linked_ticket_appends_bot_message(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    ticket = _ticket(scope)
    chat_session = _chat_session(scope, ticket_id=ticket.id)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))

    result = await chat_tools.schedule_visit(
        ctx, ticket_id=ticket.id, preferred_date="2026-09-01", notes="Morning preferred"
    )

    assert result["scheduled"] is True
    session.add.assert_called_once()


# ---------------------------------------------------------------------------
# 3.4 — escalate_to_human: anonymous session never creates a ticket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_anonymous_no_ticket(monkeypatch) -> None:
    scope = OrgScope.for_anonymous_chat(uuid4())
    chat_session = _chat_session(scope, user_id=None, ticket_id=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    create_ticket_mock = AsyncMock()
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)

    result = await chat_tools.escalate_to_human(ctx, reason="need help")

    assert chat_session.status == ChatSessionStatus.ESCALATED
    assert chat_session.ticket_id is None
    create_ticket_mock.assert_not_awaited()
    assert result["ticket_id"] is None
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# 3.4b/3.5c/3.6b — stage 7 analytics: escalate_to_human sets
# `chat_session.escalated_at` exactly once, across all 3 entry points
# (anonymous, identified-no-ticket, identified-with-ticket). A second call
# on an already-escalated session must NOT overwrite the original timestamp
# — it backs the `chat_escalation_rate` daily metric, so its value must
# stay the FIRST escalation moment, never a later re-escalation attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_anonymous_sets_escalated_at(monkeypatch) -> None:
    scope = OrgScope.for_anonymous_chat(uuid4())
    chat_session = _chat_session(scope, user_id=None, ticket_id=None, escalated_at=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock())

    await chat_tools.escalate_to_human(ctx, reason="need help")

    assert chat_session.escalated_at is not None


@pytest.mark.asyncio
async def test_escalate_identified_no_ticket_sets_escalated_at(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope, ticket_id=None, escalated_at=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    ticket = _ticket(scope)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(chat_tools, "_find_general_category_id", AsyncMock(return_value=None))
    monkeypatch.setattr(TicketRepository, "update", AsyncMock(return_value=ticket))

    await chat_tools.escalate_to_human(ctx, reason="urgent")

    assert chat_session.escalated_at is not None


@pytest.mark.asyncio
async def test_escalate_identified_with_ticket_sets_escalated_at(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    ticket_id = uuid4()
    chat_session = _chat_session(scope, ticket_id=ticket_id, escalated_at=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock())
    monkeypatch.setattr(
        TicketRepository, "update", AsyncMock(return_value=_ticket(scope, id=ticket_id))
    )

    await chat_tools.escalate_to_human(ctx, reason="urgent")

    assert chat_session.escalated_at is not None


@pytest.mark.asyncio
async def test_escalate_second_call_does_not_overwrite_escalated_at(monkeypatch) -> None:
    """An already-escalated session (e.g. re-escalated by the LLM, or a
    human agent re-triggering the tool) must keep its ORIGINAL
    `escalated_at` — the metric measures time-to-first-escalation, not the
    most recent attempt."""
    scope = _scope(role=UserRole.TENANT)
    ticket_id = uuid4()
    original_escalated_at = datetime(2026, 1, 1, tzinfo=UTC)
    chat_session = _chat_session(
        scope, ticket_id=ticket_id, escalated_at=original_escalated_at
    )
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock())
    monkeypatch.setattr(
        TicketRepository, "update", AsyncMock(return_value=_ticket(scope, id=ticket_id))
    )

    await chat_tools.escalate_to_human(ctx, reason="still stuck")

    assert chat_session.escalated_at == original_escalated_at


# ---------------------------------------------------------------------------
# 3.5 — escalate_to_human: identified session with no ticket creates then
# escalates via a DIRECT repository update, never ticket_service.update_ticket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_identified_no_ticket_creates_then_escalates(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope, ticket_id=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    ticket = _ticket(scope)
    create_ticket_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    update_ticket_mock = AsyncMock()
    monkeypatch.setattr(ticket_service, "update_ticket", update_ticket_mock)
    monkeypatch.setattr(chat_tools, "_find_general_category_id", AsyncMock(return_value=None))
    repo_update_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(TicketRepository, "update", repo_update_mock)

    result = await chat_tools.escalate_to_human(ctx, reason="urgent")

    create_ticket_mock.assert_awaited_once()
    _, create_kwargs = create_ticket_mock.call_args
    assert create_kwargs["property_id"] is None
    assert create_kwargs["contract_id"] is None
    update_ticket_mock.assert_not_awaited()
    repo_update_mock.assert_awaited_once_with(ticket.id, status=TicketStatus.ESCALATED)
    assert chat_session.ticket_id == ticket.id
    assert chat_session.status == ChatSessionStatus.ESCALATED
    assert result["ticket_id"] == str(ticket.id)


# ---------------------------------------------------------------------------
# 3.5b — stage 5 multichannel: escalation tickets are attributed to the
# actual chat session's channel, never hardcoded to WEB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel", [TicketChannel.WEB, TicketChannel.EMAIL, TicketChannel.WHATSAPP]
)
async def test_escalate_to_human_uses_the_chat_sessions_own_channel(monkeypatch, channel) -> None:
    scope = _scope(role=UserRole.TENANT)
    chat_session = _chat_session(scope, ticket_id=None, channel=channel)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    ticket = _ticket(scope)
    create_ticket_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    monkeypatch.setattr(chat_tools, "_find_general_category_id", AsyncMock(return_value=None))
    monkeypatch.setattr(TicketRepository, "update", AsyncMock(return_value=ticket))

    await chat_tools.escalate_to_human(ctx, reason="urgent")

    _, create_kwargs = create_ticket_mock.call_args
    assert create_kwargs["channel"] == channel


# ---------------------------------------------------------------------------
# 3.6 — escalate_to_human: identified session with an existing ticket never
# creates a duplicate ticket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_identified_with_ticket_no_duplicate(monkeypatch) -> None:
    scope = _scope(role=UserRole.TENANT)
    ticket_id = uuid4()
    chat_session = _chat_session(scope, ticket_id=ticket_id)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    create_ticket_mock = AsyncMock()
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    repo_update_mock = AsyncMock(return_value=_ticket(scope, id=ticket_id))
    monkeypatch.setattr(TicketRepository, "update", repo_update_mock)

    result = await chat_tools.escalate_to_human(ctx, reason="urgent")

    create_ticket_mock.assert_not_awaited()
    repo_update_mock.assert_awaited_once_with(ticket_id, status=TicketStatus.ESCALATED)
    assert chat_session.status == ChatSessionStatus.ESCALATED
    assert result["ticket_id"] == str(ticket_id)


# ---------------------------------------------------------------------------
# 3.8 — tool registry / schemas plumbing consumed by PR4's chat.py.
# ---------------------------------------------------------------------------


def test_available_tool_names_anonymous_session_only_escalate() -> None:
    scope = OrgScope.for_anonymous_chat(uuid4())
    chat_session = _chat_session(scope, user_id=None)

    assert chat_tools.available_tool_names(chat_session) == ["escalate_to_human"]


def test_available_tool_names_identified_session_all_tools() -> None:
    scope = _scope()
    chat_session = _chat_session(scope)

    names = chat_tools.available_tool_names(chat_session)

    assert set(names) == {
        "check_ticket_status",
        "create_ticket",
        "schedule_visit",
        "escalate_to_human",
    }


def test_tool_schemas_cover_every_handler() -> None:
    schema_names = {schema["function"]["name"] for schema in chat_tools.TOOL_SCHEMAS}

    assert schema_names == set(chat_tools._TOOL_HANDLERS.keys())
