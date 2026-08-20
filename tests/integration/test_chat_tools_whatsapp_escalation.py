"""Confirmation tests for `app.services.chat_tools.escalate_to_human` on a
WhatsApp-originated chat session (stage 5 — multichannel, PR5 — WhatsApp
worker + outbound).

Requirement under test: "Correct Channel Attribution and Escalation" —
WhatsApp escalation MUST succeed for auto-provisioned pending senders, and
the resulting ticket must carry `channel=whatsapp`, regardless of the
sender's `UserStatus` (PENDING senders are the common case for a brand-new
WhatsApp contact who has never completed any invite/signup flow — see
`app.services.channel_identity.resolve_whatsapp_sender_identity`).

`ticket_service.create_ticket` is monkeypatched at its module boundary
(`AsyncSession` is mocked; no live Postgres in this sandbox) — the same
boundary `tests/unit/test_chat_tools.py`'s own
`test_escalate_to_human_uses_the_chat_sessions_own_channel` already
establishes for this exact assertion. This module's addition is PENDING-
sender coverage specifically (that parametrized unit test does not vary
sender `status` at all — its `ChatSession`/`OrgScope` fixtures carry no
concept of sender status in the first place), run through the full
`ChatToolContext` -> `chat_tools.escalate_to_human` path exercised for a
`channel=whatsapp` session end to end.

Per design: this is intended to PASS with ZERO new implementation — PR1's
`channel=ctx.chat_session.channel` fix plus PR4's `channel=TicketChannel.
WHATSAPP` session creation are both already in place. A failure here would
mean a real gap, not an expected RED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.scope import OrgScope
from app.models.chat_session import ChatSession
from app.models.enums import (
    ChatSessionStatus,
    TicketChannel,
    TicketStatus,
    UserRole,
    UserStatus,
)
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository
from app.services import chat_tools, ticket_service
from app.services.chat_tools import ChatToolContext


def _pending_whatsapp_sender_scope(**overrides) -> OrgScope:
    """The scope for an auto-provisioned WhatsApp sender — the sentinel
    facts that matter here are `role=TENANT` (a real, non-anonymous
    principal) and a real `user_id`; `UserStatus.PENDING` itself lives on
    the `User` row, not `OrgScope`, but this scope models exactly what
    `OrgScope.from_principal(pending_user)` would produce for one."""
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.TENANT)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _whatsapp_chat_session(scope: OrgScope, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
        ticket_id=None,
        status=ChatSessionStatus.ACTIVE,
        low_confidence_streak=0,
        last_activity_at=datetime.now(UTC),
        channel=TicketChannel.WHATSAPP,
        channel_metadata={
            "wa_id": "15550001111",
            "phone_number_id": "1234567890",
            "profile_name": "Jane",
            "last_inbound_at": datetime.now(UTC).isoformat(),
            "processed_message_ids": ["wamid.NEW"],
            "deferred_replies": [],
        },
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _ticket(scope: OrgScope, *, channel: TicketChannel, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        user_id=scope.user_id,
        title="Chat escalation",
        status=TicketStatus.OPEN,
        category_id=None,
        channel=channel,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Ticket(**defaults)


# ---------------------------------------------------------------------------
# 5.7/5.8 — WhatsApp escalation tags the ticket `channel=whatsapp`, for any
# sender status, including the common brand-new-contact PENDING case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_status", [UserStatus.PENDING, UserStatus.ACTIVE])
async def test_whatsapp_escalation_tags_ticket_channel_whatsapp(
    monkeypatch, sender_status
) -> None:
    scope = _pending_whatsapp_sender_scope()
    chat_session = _whatsapp_chat_session(scope, ticket_id=None)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)

    ticket = _ticket(scope, channel=TicketChannel.WHATSAPP)
    create_ticket_mock = AsyncMock(return_value=ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    monkeypatch.setattr(chat_tools, "_find_general_category_id", AsyncMock(return_value=None))
    monkeypatch.setattr(TicketRepository, "update", AsyncMock(return_value=ticket))

    result = await chat_tools.escalate_to_human(ctx, reason="need a human, urgent")

    # Proves the FULL escalate_to_human path (not just the create_ticket
    # call args): the ticket_service call was made with channel=whatsapp,
    # AND the resulting ticket object (what the caller/LLM sees) itself
    # carries channel=whatsapp, AND the session/ticket linkage completed.
    _, create_kwargs = create_ticket_mock.call_args
    assert create_kwargs["channel"] == TicketChannel.WHATSAPP
    assert ticket.channel == TicketChannel.WHATSAPP
    assert result["escalated"] is True
    assert result["ticket_id"] == str(ticket.id)
    assert chat_session.ticket_id == ticket.id
    assert chat_session.status == ChatSessionStatus.ESCALATED


@pytest.mark.asyncio
async def test_whatsapp_escalation_with_existing_linked_ticket_reuses_its_channel(
    monkeypatch,
) -> None:
    """Identified sender, already has a linked ticket: no second ticket is
    created (per `escalate_to_human`'s own contract) — the existing
    ticket's channel (already whatsapp, from its own original creation)
    is simply carried forward, never re-derived or overwritten."""
    scope = _pending_whatsapp_sender_scope()
    ticket = _ticket(scope, channel=TicketChannel.WHATSAPP)
    chat_session = _whatsapp_chat_session(scope, ticket_id=ticket.id)
    session = AsyncMock()
    session.flush = AsyncMock()
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)

    create_ticket_mock = AsyncMock()
    monkeypatch.setattr(ticket_service, "create_ticket", create_ticket_mock)
    monkeypatch.setattr(TicketRepository, "update", AsyncMock(return_value=ticket))

    result = await chat_tools.escalate_to_human(ctx, reason="still stuck")

    create_ticket_mock.assert_not_awaited()
    assert result["ticket_id"] == str(ticket.id)
    assert chat_session.status == ChatSessionStatus.ESCALATED
