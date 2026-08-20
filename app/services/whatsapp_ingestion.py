"""WhatsApp ingestion — the per-message inbound-webhook unit of work (stage
5 — multichannel, PR4 — WhatsApp webhook).

Given an already-verified, already-org-resolved WhatsApp Cloud API webhook
`contact`/`message` pair (one message from `entry[].changes[].value.
messages[]`, plus its matching `entry[].changes[].value.contacts[0]` —
the caller/route loops over `messages[]`, this module processes exactly
ONE message per call), decide:

- REPLAYED (isolation invariant I5): the message's `wamid` is already in
  the resolved session's `channel_metadata["processed_message_ids"]` ring
  buffer — a pure no-op, nothing written.
- UNSUPPORTED_MESSAGE_TYPE: anything other than `type == "text"` — logged
  and skipped before any DB access, never crashes.
- APPENDED_TO_ESCALATED_TICKET: the resolved session is `ESCALATED` — the
  message is appended to its linked ticket's thread
  (`message_service.create_message`) and the bot is never invoked. Actual
  reply delivery is PR5's concern; this module only classifies the case.
- RATE_LIMITED: `chat_rate_limit.check_and_increment` rejected this session
  — dropped (never propagated as an HTTP error, unlike the widget's own
  429: a webhook must still ack 200 regardless).
- BOT_REPLY_QUEUED: a genuinely new message on an ACTIVE, non-escalated
  session — the caller (the webhook route) enqueues the
  `process_whatsapp_message` arq job for it (PR5 implements the job body).

Session get-or-create, keyed by `(organization_id, wa_id)`: an existing
`ESCALATED` session is ALWAYS reused (a human agent may be conversing on it
regardless of how idle it has gone dormant); an existing `ACTIVE` session is
reused only if its `channel_metadata["last_inbound_at"]` is within
`settings.whatsapp_session_idle_minutes`; anything else (no existing
session, an expired `ACTIVE` one, or a `CLOSED` one) starts a fresh session.

Sender resolution (`app.services.channel_identity.resolve_whatsapp_sender_
identity`) runs FIRST, before any session lookup — deliberately left
UNCAUGHT here: a `CrossOrgSenderConflict` (isolation invariant I4) must
propagate all the way to the caller (the webhook route), which logs it at
WARNING and continues to the next message in the batch. This module must
never swallow it or write anything on that path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, TicketChannel
from app.models.organization import Organization
from app.repositories.chat_session_repository import ChatSessionRepository
from app.services import channel_identity, chat_rate_limit, message_service
from app.services.chat_rate_limit import ChatRateLimitExceededError
from app.services.channel_metadata import contains, with_appended, with_values

logger = logging.getLogger(__name__)

# Ring-buffer cap for `channel_metadata["processed_message_ids"]` — mirrors
# `app.workers.email`'s own dedup-list bookkeeping precedent (unbounded
# growth is never acceptable for a JSONB column read on every inbound
# message).
_PROCESSED_MESSAGE_IDS_CAP = 50


class WhatsAppIngestionOutcome(str, Enum):
    REPLAYED = "replayed"
    UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
    APPENDED_TO_ESCALATED_TICKET = "appended_to_escalated_ticket"
    RATE_LIMITED = "rate_limited"
    BOT_REPLY_QUEUED = "bot_reply_queued"


@dataclass(frozen=True, slots=True)
class WhatsAppIngestionResult:
    """The outcome of `ingest_inbound_whatsapp_message`, plus the session
    it acted on (`None` only for `UNSUPPORTED_MESSAGE_TYPE`, which never
    resolves/creates one) and the inbound message's `wamid`/text — the
    webhook route reads `outcome`/`chat_session`/`wamid`/`text_body` off
    this to decide whether to enqueue the `process_whatsapp_message` arq
    job (only for `BOT_REPLY_QUEUED`)."""

    outcome: WhatsAppIngestionOutcome
    chat_session: ChatSession | None
    wamid: str | None
    text_body: str | None = None


def _extract_wa_id(contact: dict[str, Any]) -> str:
    return contact["wa_id"]


def _extract_profile_name(contact: dict[str, Any]) -> str | None:
    return (contact.get("profile") or {}).get("name")


def _extract_wamid(message: dict[str, Any]) -> str:
    return message["id"]


def _extract_text_body(message: dict[str, Any]) -> str:
    return (message.get("text") or {}).get("body", "")


def _is_within_idle_window(last_inbound_at_raw: Any, *, idle_minutes: int) -> bool:
    if not last_inbound_at_raw:
        return False
    try:
        last_inbound_at = datetime.fromisoformat(last_inbound_at_raw)
    except (TypeError, ValueError):
        return False
    return datetime.now(UTC) - last_inbound_at <= timedelta(minutes=idle_minutes)


async def _find_existing_session(
    session: AsyncSession, *, organization_id, wa_id: str
) -> ChatSession | None:
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.organization_id == organization_id,
            ChatSession.channel_metadata["wa_id"].astext == wa_id,
        )
        .order_by(ChatSession.last_activity_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def ingest_inbound_whatsapp_message(
    session: AsyncSession,
    *,
    organization: Organization,
    phone_number_id: str,
    contact: dict[str, Any],
    message: dict[str, Any],
    redis: Any,
) -> WhatsAppIngestionResult:
    """Ingest one verified, org-resolved inbound WhatsApp message under the
    get-or-create/dedup/escalation rules described in this module's
    docstring."""
    settings = get_settings()
    wamid = _extract_wamid(message)
    message_type = message.get("type")

    if message_type != "text":
        logger.warning(
            "inbound WhatsApp webhook: unsupported message type %r (wamid=%s); skipping",
            message_type,
            wamid,
        )
        return WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.UNSUPPORTED_MESSAGE_TYPE,
            chat_session=None,
            wamid=wamid,
        )

    wa_id = _extract_wa_id(contact)
    profile_name = _extract_profile_name(contact)
    text_body = _extract_text_body(message)

    sender = await channel_identity.resolve_whatsapp_sender_identity(
        session, organization_id=organization.id, wa_id=wa_id, display_name=profile_name
    )
    scope = OrgScope.from_principal(sender)
    repo = ChatSessionRepository(session, scope)

    existing = await _find_existing_session(session, organization_id=organization.id, wa_id=wa_id)

    reuse = False
    if existing is not None:
        if existing.status == ChatSessionStatus.ESCALATED:
            reuse = True
        elif existing.status == ChatSessionStatus.ACTIVE and _is_within_idle_window(
            (existing.channel_metadata or {}).get("last_inbound_at"),
            idle_minutes=settings.whatsapp_session_idle_minutes,
        ):
            reuse = True

    now_iso = datetime.now(UTC).isoformat()

    if reuse:
        chat_session = existing
        if contains(chat_session.channel_metadata, "processed_message_ids", wamid):
            return WhatsAppIngestionResult(
                outcome=WhatsAppIngestionOutcome.REPLAYED, chat_session=chat_session, wamid=wamid
            )
        chat_session.channel_metadata = with_appended(
            chat_session.channel_metadata,
            "processed_message_ids",
            wamid,
            cap=_PROCESSED_MESSAGE_IDS_CAP,
        )
        chat_session.channel_metadata = with_values(
            chat_session.channel_metadata, last_inbound_at=now_iso
        )
        chat_session.last_activity_at = datetime.now(UTC)
    else:
        chat_session = repo.add(
            user_id=sender.id,
            channel=TicketChannel.WHATSAPP,
            channel_metadata={
                "wa_id": wa_id,
                "phone_number_id": phone_number_id,
                "profile_name": profile_name,
                "last_inbound_at": now_iso,
                "processed_message_ids": [wamid],
                "deferred_replies": [],
            },
        )
        await session.flush()

    if chat_session.status == ChatSessionStatus.ESCALATED:
        await message_service.create_message(
            session, scope=scope, ticket_id=chat_session.ticket_id, content=text_body
        )
        return WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.APPENDED_TO_ESCALATED_TICKET,
            chat_session=chat_session,
            wamid=wamid,
            text_body=text_body,
        )

    try:
        await chat_rate_limit.check_and_increment(redis, chat_session_id=chat_session.id)
    except ChatRateLimitExceededError:
        logger.warning(
            "inbound WhatsApp webhook: chat session %s exceeded its rate limit; dropping message",
            chat_session.id,
        )
        return WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.RATE_LIMITED, chat_session=chat_session, wamid=wamid
        )

    return WhatsAppIngestionResult(
        outcome=WhatsAppIngestionOutcome.BOT_REPLY_QUEUED,
        chat_session=chat_session,
        wamid=wamid,
        text_body=text_body,
    )
