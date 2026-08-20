"""Async arq job for stage-5 multichannel outbound email replies (PR3 —
email outbound; design ADR-15).

`send_ticket_email_reply` is enqueued by `app.api.v1.tickets.create_message`
whenever an agent/admin posts a reply on a `channel=email` ticket — see
`app.services.message_service.create_message`'s `outbound_email_required`
flag, the non-mapped signal the API layer reads to decide whether to enqueue
this job at all (mirroring `app.api.v1.tickets.create_ticket`'s
`enqueue_classification` hook).

Like `app.workers.classification.classify_ticket`, this job opens its own
session (`ctx["session_factory"]`) and loads its rows under
`OrgScope.for_background_worker(organization_id)` — read-only, and the
reply is a system action with no FK write to `users.id`.

Idempotency: the outbound `Message-ID` is DETERMINISTIC, derived purely from
`message.id` (`<helpdesk-{message.id}@{settings.email_message_id_domain}>`),
never randomly generated — so a duplicate arq delivery of this exact job
(arq's at-least-once semantics) is a safe no-op, checked via
`app.services.channel_metadata.contains` against
`ticket.channel_metadata["message_ids"]` before ever sending. The
Subject/In-Reply-To/References thread headers reuse
`app.services.email_threading.build_reply_headers` verbatim (PR2 — email
inbound already implements it, tagged there for PR3 to reuse as-is).
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Any
from uuid import UUID

import aiosmtplib
from arq import Retry, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.repositories.message_repository import MessageRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.ticket_repository import TicketRepository
from app.services import mailer_service
from app.services.channel_metadata import contains, with_appended, with_values
from app.services.email_threading import build_reply_headers

logger = logging.getLogger(__name__)

# `aiosmtplib.SMTPConnectError`/`SMTPConnectTimeoutError`/`SMTPTimeoutError`/
# `SMTPReadTimeoutError` (plus any bare `OSError`, e.g. a DNS/socket
# failure) are treated as TRANSIENT — worth retrying via arq's own backoff.
# Every other `aiosmtplib.SMTPException` (a rejected recipient, an auth
# failure, a permanent 5xx response, ...) is treated as PERMANENT: logged
# and swallowed, never retried.
_TRANSIENT_SMTP_ERRORS = (
    aiosmtplib.SMTPConnectError,
    aiosmtplib.SMTPConnectTimeoutError,
    aiosmtplib.SMTPTimeoutError,
    aiosmtplib.SMTPReadTimeoutError,
    OSError,
)


async def send_ticket_email_reply(
    ctx: dict[str, Any], message_id: UUID, ticket_id: UUID, organization_id: UUID
) -> None:
    """Send the outbound email for an agent reply on a `channel=email`
    ticket, then record the deterministic Message-ID on
    `ticket.channel_metadata` so a duplicate delivery of this same job is a
    no-op.

    On a transient SMTP failure, raises `arq.Retry` with an exponential
    backoff `defer` (mirroring `classify_ticket`'s convention exactly). On a
    permanent SMTP failure, logs a warning and returns normally — arq must
    not retry a permanent failure.
    """
    session_factory = ctx["session_factory"]
    settings = get_settings()
    scope = OrgScope.for_background_worker(organization_id)

    async with session_factory() as session:
        message = await MessageRepository(session, scope).get_or_404(message_id)
        ticket = await TicketRepository(session, scope).get_or_404(ticket_id)

        outbound_message_id = f"<helpdesk-{message.id}@{settings.email_message_id_domain}>"
        if contains(ticket.channel_metadata, "message_ids", outbound_message_id):
            # Already sent by an earlier delivery of this same job.
            return

        organization = await OrganizationRepository(session).get_or_404(organization_id)

        metadata = ticket.channel_metadata or {}
        message_ids = list(metadata.get("message_ids", []))
        in_reply_to = metadata.get("last_outbound_message_id") or (
            message_ids[-1] if message_ids else outbound_message_id
        )
        subject = metadata.get("subject_normalized") or ticket.title

        headers = build_reply_headers(
            ticket_id=ticket.id,
            original_subject=subject,
            in_reply_to_message_id=in_reply_to,
            references_message_ids=message_ids,
        )

        from_address = organization.support_email_address or settings.email_from_fallback_address

        email_message = EmailMessage()
        email_message["From"] = from_address
        email_message["Reply-To"] = from_address
        email_message["To"] = metadata.get("sender_address")
        email_message["Subject"] = headers["Subject"]
        email_message["In-Reply-To"] = headers["In-Reply-To"]
        email_message["References"] = headers["References"]
        email_message["Message-ID"] = outbound_message_id
        email_message.set_content(message.content)

        try:
            await mailer_service.send_message(email_message)
        except _TRANSIENT_SMTP_ERRORS as exc:
            job_try = ctx.get("job_try", 1)
            raise Retry(defer=2**job_try) from exc
        except aiosmtplib.SMTPException:
            logger.warning(
                "permanent SMTP failure sending reply for message %s; not retrying",
                message_id,
                exc_info=True,
            )
            return

        ticket.channel_metadata = with_appended(
            ticket.channel_metadata, "message_ids", outbound_message_id
        )
        ticket.channel_metadata = with_values(
            ticket.channel_metadata, last_outbound_message_id=outbound_message_id
        )
        await session.commit()


async def enqueue_ticket_email_reply(
    message_id: UUID, ticket_id: UUID, organization_id: UUID
) -> None:
    """Enqueue a `send_ticket_email_reply` job for a just-created agent
    reply on a `channel=email` ticket.

    Called from the API layer via `fastapi.BackgroundTasks`, right after the
    creating request commits — see `app.api.v1.tickets.create_message`.
    Opens a short-lived Redis connection pool scoped to this single enqueue
    call, mirroring `app.workers.classification.enqueue_classification`
    exactly. The pool is always closed, even if the enqueue itself raises.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job(
            "send_ticket_email_reply", message_id, ticket_id, organization_id
        )
    finally:
        await redis.aclose()
