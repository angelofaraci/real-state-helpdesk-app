"""Tests for `app.services.email_ingestion` — the inbound-email matching
ladder (stage 5 — multichannel, PR2 — email inbound; design ADR-7).

`AsyncSession` is mocked at its own boundary (no live Postgres in this
sandbox — see `tests/unit/test_channel_identity.py`'s module docstring for
the established rationale). `channel_identity.resolve_email_sender_identity`
(tested on its own in `tests/unit/test_channel_identity.py`) and the
`ticket_service`/`message_service` write functions (tested on their own in
`tests/unit/test_ticket_service_create.py`/`tests/unit/test_message_
service.py`) are monkeypatched at their module boundary, following the
exact pattern `tests/api/test_messages_endpoints.py`/`test_tickets_
endpoints.py` already establish — this test module owns proving the
MATCHING LADDER's control flow (which of the 4 steps fires, and in what
order), not re-proving those already-tested collaborators.

Isolation invariant I5: a replayed provider Message-ID must be a pure no-op
— no new ticket, no new message, nothing written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.enums import TicketChannel, TicketStatus, UserRole, UserStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.models.user import User
from app.services import channel_identity, message_service, ticket_service
from app.services.channel_identity import CrossOrgSenderConflict
from app.services.email_ingestion import EmailIngestionOutcome, ingest_inbound_email
from app.services.email_provider import InboundEmail
from app.services.email_threading import normalize_subject


def _execute_scalars(items: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _execute_scalar(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _session_with_executes(*results) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


def _org(**overrides) -> Organization:
    defaults = dict(id=uuid4(), name="Landlord Co", support_email_address="support@landlord.example")
    defaults.update(overrides)
    return Organization(**defaults)


def _sender(*, organization_id, email="jane@example.com") -> User:
    return User(
        id=uuid4(),
        organization_id=organization_id,
        name="Jane Tenant",
        email=email,
        role=UserRole.TENANT,
        status=UserStatus.ACTIVE,
        password_hash="hashed",
    )


def _ticket(*, organization_id, user_id, channel_metadata=None, created_at=None, status=TicketStatus.OPEN, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=user_id,
        title="Leaking faucet",
        description=None,
        channel=TicketChannel.EMAIL,
        status=status,
        channel_metadata=channel_metadata,
    )
    defaults.update(overrides)
    ticket = Ticket(**defaults)
    ticket.created_at = created_at or datetime.now(UTC)
    return ticket


def _inbound(**overrides) -> InboundEmail:
    defaults = dict(
        provider_message_id="<inbound-1@example.com>",
        in_reply_to=None,
        references=[],
        from_address="jane@example.com",
        from_display_name="Jane Tenant",
        to_address="support@landlord.example",
        subject="Leaking faucet",
        text_body="It is still leaking.",
    )
    defaults.update(overrides)
    return InboundEmail(**defaults)


# ---------------------------------------------------------------------------
# Step 0 — idempotency (I5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_message_id_is_idempotent_and_writes_nothing(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    existing = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={"message_ids": ["<inbound-1@example.com>"]},
    )
    session = _session_with_executes(_execute_scalars([existing]))
    mocked_create_ticket = AsyncMock()
    mocked_create_message = AsyncMock()
    monkeypatch.setattr(ticket_service, "create_ticket", mocked_create_ticket)
    monkeypatch.setattr(message_service, "create_message", mocked_create_message)

    inbound = _inbound(provider_message_id="<inbound-1@example.com>")

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.REPLAYED
    assert result.ticket is existing
    mocked_create_ticket.assert_not_called()
    mocked_create_message.assert_not_called()
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# Step 1 — In-Reply-To/References match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matches_by_in_reply_to_header_and_appends_message(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    ticket = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={"message_ids": ["<inbound-0@example.com>"]},
    )
    session = _session_with_executes(_execute_scalars([ticket]), _execute_scalar(ticket))
    mocked_create_message = AsyncMock()
    monkeypatch.setattr(message_service, "create_message", mocked_create_message)

    inbound = _inbound(
        provider_message_id="<inbound-1@example.com>", in_reply_to="<inbound-0@example.com>"
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.MESSAGE_APPENDED
    assert result.ticket is ticket
    assert ticket.channel_metadata["message_ids"] == [
        "<inbound-0@example.com>",
        "<inbound-1@example.com>",
    ]
    mocked_create_message.assert_awaited_once()
    kwargs = mocked_create_message.call_args.kwargs
    assert kwargs["ticket_id"] == ticket.id
    assert kwargs["content"] == inbound.text_body


@pytest.mark.asyncio
async def test_matches_by_references_header_when_in_reply_to_is_missing(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    ticket = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={"message_ids": ["<inbound-0@example.com>"]},
    )
    session = _session_with_executes(_execute_scalars([ticket]), _execute_scalar(ticket))
    monkeypatch.setattr(message_service, "create_message", AsyncMock())

    inbound = _inbound(
        provider_message_id="<inbound-2@example.com>",
        in_reply_to=None,
        references=["<inbound-0@example.com>"],
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.MESSAGE_APPENDED
    assert result.ticket is ticket


# ---------------------------------------------------------------------------
# Step 2 — subject-tag match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matches_by_subject_tag_when_exactly_one_hit(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    ticket = _ticket(organization_id=org.id, user_id=sender.id, channel_metadata={"message_ids": []})
    other = _ticket(organization_id=org.id, user_id=sender.id, channel_metadata={"message_ids": []})
    tag = str(ticket.id)[:8]
    session = _session_with_executes(_execute_scalars([ticket, other]), _execute_scalar(ticket))
    monkeypatch.setattr(message_service, "create_message", AsyncMock())

    inbound = _inbound(subject=f"Re: Leaking faucet [#{tag}]", in_reply_to=None, references=[])

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.MESSAGE_APPENDED
    assert result.ticket is ticket


@pytest.mark.asyncio
async def test_ambiguous_subject_tag_falls_through_to_sender_subject_fallback(monkeypatch) -> None:
    """A tag prefix cannot disambiguate between two matching tickets (an
    astronomically unlikely 8-hex-char collision, but the ladder must still
    fail closed rather than pick one arbitrarily): falls through to step 3,
    which finds no match either (no candidate has the sender/subject
    fields set), so a new ticket is created instead."""
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    shared_tag = "a1b2c3d4"
    ticket_a = _ticket(
        id=f"{shared_tag}-0000-0000-0000-000000000000",
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={"message_ids": []},
    )
    ticket_b = _ticket(
        id=f"{shared_tag}-1111-0000-0000-000000000000",
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={"message_ids": []},
    )
    session = _session_with_executes(_execute_scalars([ticket_a, ticket_b]))
    new_ticket = _ticket(organization_id=org.id, user_id=sender.id)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=new_ticket))

    inbound = _inbound(
        subject=f"Re: Leaking faucet [#{shared_tag}]",
        in_reply_to=None,
        references=[],
        from_address="someone-else@example.com",
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.TICKET_CREATED


# ---------------------------------------------------------------------------
# Step 3 — sender + subject + 30-day fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_falls_back_to_sender_and_subject_match_within_the_window(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id, email="jane@example.com")
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    recent_ticket = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={
            "sender_address": "jane@example.com",
            "subject_normalized": normalize_subject("Leaking faucet"),
            "message_ids": [],
        },
        created_at=datetime.now(UTC) - timedelta(days=5),
    )
    session = _session_with_executes(
        _execute_scalars([recent_ticket]), _execute_scalar(recent_ticket)
    )
    monkeypatch.setattr(message_service, "create_message", AsyncMock())

    inbound = _inbound(
        from_address="jane@example.com", subject="Leaking faucet", in_reply_to=None, references=[]
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.MESSAGE_APPENDED
    assert result.ticket is recent_ticket


@pytest.mark.asyncio
async def test_sender_subject_fallback_ignores_tickets_older_than_the_window(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id, email="jane@example.com")
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    stale_ticket = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        channel_metadata={
            "sender_address": "jane@example.com",
            "subject_normalized": normalize_subject("Leaking faucet"),
            "message_ids": [],
        },
        created_at=datetime.now(UTC) - timedelta(days=31),
    )
    session = _session_with_executes(_execute_scalars([stale_ticket]))
    new_ticket = _ticket(organization_id=org.id, user_id=sender.id)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=new_ticket))

    inbound = _inbound(
        from_address="jane@example.com", subject="Leaking faucet", in_reply_to=None, references=[]
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.TICKET_CREATED


@pytest.mark.asyncio
async def test_sender_subject_fallback_ignores_closed_tickets(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id, email="jane@example.com")
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    closed_ticket = _ticket(
        organization_id=org.id,
        user_id=sender.id,
        status=TicketStatus.CLOSED,
        channel_metadata={
            "sender_address": "jane@example.com",
            "subject_normalized": normalize_subject("Leaking faucet"),
            "message_ids": [],
        },
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    session = _session_with_executes(_execute_scalars([closed_ticket]))
    new_ticket = _ticket(organization_id=org.id, user_id=sender.id)
    monkeypatch.setattr(ticket_service, "create_ticket", AsyncMock(return_value=new_ticket))

    inbound = _inbound(
        from_address="jane@example.com", subject="Leaking faucet", in_reply_to=None, references=[]
    )

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.TICKET_CREATED


# ---------------------------------------------------------------------------
# Step 4 — no match: new ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_a_new_ticket_when_nothing_matches(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(return_value=sender)
    )
    session = _session_with_executes(_execute_scalars([]))
    new_ticket = _ticket(organization_id=org.id, user_id=sender.id, channel_metadata=None)
    mocked_create_ticket = AsyncMock(return_value=new_ticket)
    monkeypatch.setattr(ticket_service, "create_ticket", mocked_create_ticket)

    inbound = _inbound(subject="Brand new issue", provider_message_id="<inbound-9@example.com>")

    result = await ingest_inbound_email(session, organization=org, inbound=inbound)

    assert result.outcome == EmailIngestionOutcome.TICKET_CREATED
    assert result.ticket is new_ticket
    assert new_ticket.channel_metadata["message_ids"] == ["<inbound-9@example.com>"]
    assert new_ticket.channel_metadata["sender_address"] == inbound.from_address.lower()
    assert new_ticket.channel_metadata["subject_normalized"] == normalize_subject(inbound.subject)
    assert new_ticket.channel_metadata["last_outbound_message_id"] is None
    session.flush.assert_awaited()

    mocked_create_ticket.assert_awaited_once()
    kwargs = mocked_create_ticket.call_args.kwargs
    assert kwargs["channel"] == TicketChannel.EMAIL
    assert kwargs["category_id"] is None
    assert kwargs["urgency_id"] is None
    assert kwargs["property_id"] is None
    assert kwargs["contract_id"] is None
    assert kwargs["title"] == "Brand new issue"
    assert kwargs["description"] == inbound.text_body


# ---------------------------------------------------------------------------
# Cross-org sender conflict propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_org_sender_conflict_propagates_uncaught(monkeypatch) -> None:
    """`CrossOrgSenderConflict` must propagate — the ingestion function
    must never swallow it; the caller (webhook route) is responsible for
    logging + acking 200."""
    org = _org()
    conflict = CrossOrgSenderConflict(
        organization_id=org.id, existing_user_organization_id=uuid4(), channel=TicketChannel.EMAIL
    )
    monkeypatch.setattr(
        channel_identity, "resolve_email_sender_identity", AsyncMock(side_effect=conflict)
    )
    session = AsyncMock()

    with pytest.raises(CrossOrgSenderConflict):
        await ingest_inbound_email(session, organization=org, inbound=_inbound())

    session.execute.assert_not_called()
