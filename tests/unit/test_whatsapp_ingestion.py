"""Tests for `app.services.whatsapp_ingestion` — the per-message inbound
WhatsApp unit of work (stage 5 — multichannel, PR4 — WhatsApp webhook).

`AsyncSession`/redis are mocked at their own boundary (no live Postgres in
this sandbox — see `tests/unit/test_channel_identity.py`'s module docstring
for the established rationale). `channel_identity.resolve_whatsapp_sender_
identity` and `message_service.create_message` are monkeypatched at their
module boundary, following the exact pattern
`tests/integration/test_email_ingestion.py` already establishes for the
email half — this test module owns proving the SESSION get-or-create +
dedup + escalation-branch control flow, not re-proving those already-tested
collaborators.

Isolation invariant I5: a replayed provider `wamid` must be a pure no-op —
no metadata mutation, no message appended.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, TicketChannel, UserRole, UserStatus
from app.models.message import Message
from app.models.organization import Organization
from app.models.user import User
from app.services import channel_identity, chat_rate_limit, message_service
from app.services.whatsapp_ingestion import (
    WhatsAppIngestionOutcome,
    ingest_inbound_whatsapp_message,
)


def _execute_scalar(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _session_with_executes(*results) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    # Real `AsyncSession.add` is SYNCHRONOUS — `ChatSessionRepository.add`
    # (via `ScopedRepository.add`) calls it directly, so an unconstrained
    # `AsyncMock` child would return an unawaited coroutine.
    session.add = MagicMock()
    return session


def _redis() -> MagicMock:
    """`chat_rate_limit.check_and_increment` calls `redis.pipeline()`
    SYNCHRONOUSLY (mirroring a real `redis.asyncio.Redis`'s own
    `pipeline()` method) — the outer object must be a plain `MagicMock`,
    not an `AsyncMock`, or `.pipeline()` itself becomes an unawaited
    coroutine."""
    pipeline = MagicMock()
    pipeline.incr = MagicMock()
    pipeline.expire = MagicMock()
    pipeline.execute = AsyncMock(return_value=[1])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipeline)
    return redis


def _org(**overrides) -> Organization:
    defaults = dict(id=uuid4(), name="Landlord Co", whatsapp_phone_number_id="1234567890")
    defaults.update(overrides)
    return Organization(**defaults)


def _sender(*, organization_id, wa_id="15550001111") -> User:
    return User(
        id=uuid4(),
        organization_id=organization_id,
        name="Jane Tenant",
        email=f"wa+{wa_id}@whatsapp.invalid",
        phone_number="+15550001111",
        role=UserRole.TENANT,
        status=UserStatus.PENDING,
        password_hash=None,
    )


def _chat_session(
    *,
    organization_id,
    user_id,
    status=ChatSessionStatus.ACTIVE,
    channel_metadata=None,
    ticket_id=None,
) -> ChatSession:
    session = ChatSession(
        id=uuid4(),
        organization_id=organization_id,
        user_id=user_id,
        ticket_id=ticket_id,
        status=status,
        channel=TicketChannel.WHATSAPP,
        channel_metadata=channel_metadata,
    )
    session.last_activity_at = datetime.now(UTC)
    return session


def _contact(*, wa_id="15550001111", name="Jane") -> dict:
    return {"wa_id": wa_id, "profile": {"name": name}}


def _message(*, wamid="wamid.ABC123", body="hi there", type_="text") -> dict:
    if type_ == "text":
        return {"id": wamid, "type": "text", "text": {"body": body}}
    return {"id": wamid, "type": type_}


@pytest.mark.asyncio
async def test_known_active_session_within_idle_window_is_reused(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    existing = _chat_session(
        organization_id=org.id,
        user_id=sender.id,
        status=ChatSessionStatus.ACTIVE,
        channel_metadata={
            "wa_id": "15550001111",
            "last_inbound_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "processed_message_ids": ["wamid.OLD"],
        },
    )
    session = _session_with_executes(_execute_scalar(existing))
    redis = _redis()

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(wamid="wamid.NEW"),
        redis=redis,
    )

    assert result.chat_session is existing
    assert result.outcome == WhatsAppIngestionOutcome.BOT_REPLY_QUEUED
    assert existing.channel_metadata["processed_message_ids"] == ["wamid.OLD", "wamid.NEW"]


@pytest.mark.asyncio
async def test_no_existing_session_creates_a_new_one(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    session = _session_with_executes(_execute_scalar(None))
    redis = _redis()

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(wamid="wamid.NEW"),
        redis=redis,
    )

    assert result.outcome == WhatsAppIngestionOutcome.BOT_REPLY_QUEUED
    assert result.chat_session is not None
    assert result.chat_session.user_id == sender.id
    assert result.chat_session.channel == TicketChannel.WHATSAPP
    assert result.chat_session.channel_metadata["wa_id"] == "15550001111"
    assert result.chat_session.channel_metadata["processed_message_ids"] == ["wamid.NEW"]
    session.add.assert_called_once()
    session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_expired_active_session_creates_a_new_session(monkeypatch) -> None:
    """An ACTIVE session whose `last_inbound_at` is older than
    `whatsapp_session_idle_minutes` must NOT be reused — a fresh session is
    created instead."""
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    idle_minutes = get_settings().whatsapp_session_idle_minutes
    stale = _chat_session(
        organization_id=org.id,
        user_id=sender.id,
        status=ChatSessionStatus.ACTIVE,
        channel_metadata={
            "wa_id": "15550001111",
            "last_inbound_at": (
                datetime.now(UTC) - timedelta(minutes=idle_minutes + 10)
            ).isoformat(),
            "processed_message_ids": ["wamid.OLD"],
        },
    )
    session = _session_with_executes(_execute_scalar(stale))
    redis = _redis()

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(wamid="wamid.NEW"),
        redis=redis,
    )

    assert result.chat_session is not stale
    assert result.chat_session.channel_metadata["processed_message_ids"] == ["wamid.NEW"]


@pytest.mark.asyncio
async def test_escalated_session_appends_to_ticket_without_invoking_bot(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    ticket_id = uuid4()
    escalated = _chat_session(
        organization_id=org.id,
        user_id=sender.id,
        status=ChatSessionStatus.ESCALATED,
        ticket_id=ticket_id,
        channel_metadata={
            "wa_id": "15550001111",
            "last_inbound_at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
            "processed_message_ids": ["wamid.OLD"],
        },
    )
    session = _session_with_executes(_execute_scalar(escalated))
    redis = _redis()
    mocked_create_message = AsyncMock(return_value=MagicMock(spec=Message))
    monkeypatch.setattr(message_service, "create_message", mocked_create_message)

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(wamid="wamid.NEW", body="please help"),
        redis=redis,
    )

    assert result.outcome == WhatsAppIngestionOutcome.APPENDED_TO_ESCALATED_TICKET
    mocked_create_message.assert_awaited_once()
    kwargs = mocked_create_message.call_args.kwargs
    assert kwargs["ticket_id"] == ticket_id
    assert kwargs["content"] == "please help"


@pytest.mark.asyncio
async def test_replayed_wamid_is_idempotent_and_writes_nothing(monkeypatch) -> None:
    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    existing = _chat_session(
        organization_id=org.id,
        user_id=sender.id,
        status=ChatSessionStatus.ACTIVE,
        channel_metadata={
            "wa_id": "15550001111",
            "last_inbound_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "processed_message_ids": ["wamid.DUP"],
        },
    )
    session = _session_with_executes(_execute_scalar(existing))
    redis = _redis()

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(wamid="wamid.DUP"),
        redis=redis,
    )

    assert result.outcome == WhatsAppIngestionOutcome.REPLAYED
    assert existing.channel_metadata["processed_message_ids"] == ["wamid.DUP"]
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_message_type_is_skipped_without_db_access(monkeypatch) -> None:
    org = _org()
    mocked_resolve = AsyncMock()
    monkeypatch.setattr(channel_identity, "resolve_whatsapp_sender_identity", mocked_resolve)
    session = AsyncMock()
    redis = _redis()

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(type_="image"),
        redis=redis,
    )

    assert result.outcome == WhatsAppIngestionOutcome.UNSUPPORTED_MESSAGE_TYPE
    session.execute.assert_not_called()
    mocked_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limited_new_message_is_dropped_without_enqueue_signal(monkeypatch) -> None:
    from app.services.chat_rate_limit import ChatRateLimitExceededError

    org = _org()
    sender = _sender(organization_id=org.id)
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(return_value=sender)
    )
    session = _session_with_executes(_execute_scalar(None))
    redis = _redis()
    monkeypatch.setattr(
        chat_rate_limit,
        "check_and_increment",
        AsyncMock(
            side_effect=ChatRateLimitExceededError(uuid4(), limit=30, retry_after=10)
        ),
    )

    result = await ingest_inbound_whatsapp_message(
        session,
        organization=org,
        phone_number_id="1234567890",
        contact=_contact(),
        message=_message(),
        redis=redis,
    )

    assert result.outcome == WhatsAppIngestionOutcome.RATE_LIMITED


@pytest.mark.asyncio
async def test_cross_org_sender_conflict_propagates_uncaught(monkeypatch) -> None:
    from app.services.channel_identity import CrossOrgSenderConflict

    org = _org()
    conflict = CrossOrgSenderConflict(
        organization_id=org.id, existing_user_organization_id=uuid4(), channel=TicketChannel.WHATSAPP
    )
    monkeypatch.setattr(
        channel_identity, "resolve_whatsapp_sender_identity", AsyncMock(side_effect=conflict)
    )
    session = AsyncMock()
    redis = _redis()

    with pytest.raises(CrossOrgSenderConflict):
        await ingest_inbound_whatsapp_message(
            session,
            organization=org,
            phone_number_id="1234567890",
            contact=_contact(),
            message=_message(),
            redis=redis,
        )

    session.execute.assert_not_called()
