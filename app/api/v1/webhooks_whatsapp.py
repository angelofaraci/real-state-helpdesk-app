"""`GET`/`POST /api/v1/webhooks/whatsapp` — inbound WhatsApp/Meta Cloud API
webhook (stage 5 — multichannel, PR4 — WhatsApp webhook).

`GET` completes Meta's verification handshake — deliberately handled
directly here rather than as a `deps_webhooks` dependency, since it needs
no signature verification/DB access of its own, just a query-param
comparison against `settings.whatsapp_verify_token`.

Design ADR-9: every BUSINESS-level outcome on `POST` acks 200 — no
matching organization, a zero-message (status-callback-only) payload, a
cross-org sender conflict on any one message, a replay, a rate-limited
drop, an escalated-ticket append, or a brand-new bot-bound message. Only an
invalid/missing `X-Hub-Signature-256` is ever rejected, and that happens
entirely inside `verify_and_parse_whatsapp_webhook`'s own `Depends` —
`InvalidWebhookSignatureError` propagates uncaught to `app.main`'s already-
registered handler (401), never a local `try`/`except` here (same idiom as
`app.api.v1.webhooks_email`).

A single `POST` body can carry MULTIPLE inbound messages
(`entry[].changes[].value.messages[]`); each is its own independent unit of
work through `whatsapp_ingestion.ingest_inbound_whatsapp_message` — a
`CrossOrgSenderConflict` (isolation invariant I4) on any one message is
logged and the loop simply continues to the next message, never aborting
the whole batch.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_chat import get_redis
from app.api.deps_webhooks import WhatsAppWebhookRequest, verify_and_parse_whatsapp_webhook
from app.core.config import get_settings
from app.core.session import get_session
from app.services import whatsapp_ingestion
from app.services.channel_identity import CrossOrgSenderConflict
from app.services.whatsapp_ingestion import WhatsAppIngestionOutcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_ACK = {"status": "ok"}


@router.get("/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta's subscription verification handshake: echo `hub.challenge`
    back verbatim when `hub.mode == "subscribe"` and `hub.verify_token`
    matches `settings.whatsapp_verify_token` exactly; 403 otherwise
    (including when `whatsapp_verify_token` itself is unconfigured)."""
    settings = get_settings()
    expected_token = settings.whatsapp_verify_token
    if (
        hub_mode == "subscribe"
        and expected_token is not None
        and hub_verify_token == expected_token
    ):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification failed")


async def enqueue_whatsapp_message_processing(
    chat_session_id: UUID, wamid: str | None, text_body: str | None
) -> None:
    """Enqueue a `process_whatsapp_message` arq job for a newly-ingested
    inbound message on an ACTIVE, non-escalated chat session.

    References the job by STRING NAME only, exactly like `app.workers.
    classification.enqueue_classification`/`app.workers.email.enqueue_
    ticket_email_reply` already do — arq resolves job names against the
    WORKER's own registry at delivery time, not against the caller's
    import graph at enqueue time, so this call is valid today even though
    `process_whatsapp_message`'s job body does not exist until a later PR.
    Opens a short-lived Redis connection pool scoped to this single enqueue
    call, mirroring those same two helpers exactly; always closed, even if
    the enqueue itself raises.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("process_whatsapp_message", chat_session_id, wamid, text_body)
    finally:
        await redis.aclose()


def _iter_messages(payload: dict[str, Any]):
    """Yield `(phone_number_id, contact, message)` for every inbound
    message across every `entry`/`change` in a webhook payload — a
    status-callback-only `value` (no `messages` key) yields nothing."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages") or []
            if not messages:
                continue
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
            contacts = value.get("contacts") or []
            contact = contacts[0] if contacts else {}
            for message in messages:
                yield phone_number_id, contact, message


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    background_tasks: BackgroundTasks,
    webhook: WhatsAppWebhookRequest = Depends(verify_and_parse_whatsapp_webhook),
    session: AsyncSession = Depends(get_session),
    redis: object = Depends(get_redis),
) -> dict[str, str]:
    """Ingest every message in one verified inbound WhatsApp webhook
    payload. Each `BOT_REPLY_QUEUED` outcome enqueues a
    `process_whatsapp_message` job via `BackgroundTasks`, mirroring
    `app.api.v1.webhooks_email.receive_email_webhook`'s own hook exactly."""
    if webhook.organization is None:
        # No org's `whatsapp_phone_number_id` matches the payload's
        # verified `phone_number_id` — a business-level outcome (ADR-9),
        # not an error.
        logger.warning(
            "inbound WhatsApp webhook: no organization matches the verified phone_number_id"
        )
        return _ACK

    for phone_number_id, contact, message in _iter_messages(webhook.payload):
        try:
            result = await whatsapp_ingestion.ingest_inbound_whatsapp_message(
                session,
                organization=webhook.organization,
                phone_number_id=phone_number_id,
                contact=contact,
                message=message,
                redis=redis,
            )
        except CrossOrgSenderConflict as exc:
            # Isolation invariant I4: never the wa_id itself in the log
            # line. Continue to the next message in this same batch rather
            # than aborting the whole webhook delivery.
            logger.warning(
                "inbound WhatsApp webhook: cross-org sender conflict "
                "organization_id=%s existing_user_organization_id=%s",
                exc.organization_id,
                exc.existing_user_organization_id,
            )
            continue

        if result.outcome == WhatsAppIngestionOutcome.BOT_REPLY_QUEUED:
            background_tasks.add_task(
                enqueue_whatsapp_message_processing,
                result.chat_session.id,
                result.wamid,
                result.text_body,
            )

    return _ACK
