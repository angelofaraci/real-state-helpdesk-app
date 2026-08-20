"""FastAPI dependencies for inbound channel webhooks (stage 5 —
multichannel).

Email half (PR2) plus the WhatsApp/Meta half (PR4): Meta Cloud API webhook
signature verification (`X-Hub-Signature-256`) + org resolution by
`whatsapp_phone_number_id`, mirroring the email dependency's own
verify-before-any-DB-read shape exactly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_chat import get_redis
from app.core.config import get_settings
from app.core.session import get_session
from app.core.webhook_signature import InvalidWebhookSignatureError, verify_meta_signature
from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository
from app.services.email_provider import EmailWebhookProvider, InboundEmail, get_email_provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailWebhookRequest:
    """The verified + parsed inbound email, plus its resolved
    organization.

    `organization` is `None` when no organization's `support_email_address`
    matches the inbound `to` address — a business-level outcome (design
    ADR-9), NOT a signature failure: the caller (the webhook route) acks
    200 for this case rather than treating it as an error.
    """

    organization: Organization | None
    email: InboundEmail


async def get_email_webhook_provider(redis: object = Depends(get_redis)) -> EmailWebhookProvider:
    """Build the configured `EmailWebhookProvider`, wiring in the shared
    Redis pool `app.api.deps_chat.get_redis` already provides (for the
    provider's single-use replay-token cache)."""
    return get_email_provider(redis=redis)


async def verify_and_parse_email_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    provider: EmailWebhookProvider = Depends(get_email_webhook_provider),
) -> EmailWebhookRequest:
    """Verify an inbound `POST /api/v1/webhooks/email` request's provider
    signature, parse it into an `InboundEmail`, and resolve which
    organization it belongs to.

    Verification happens BEFORE any database statement (isolation
    invariant I1): the raw body/form are read straight off `request` (no DB
    access of their own), `provider.verify()` runs next, and only once that
    succeeds does the org lookup touch the session at all.
    `InvalidWebhookSignatureError` is deliberately left to propagate
    uncaught — `app.main`'s registered exception handler maps it to 401,
    the same idiom every other domain exception in this app follows (never
    a local `try`/`except` here).
    """
    raw_body = await request.body()
    form = dict(await request.form())
    headers = dict(request.headers)

    await provider.verify(headers=headers, form=form, raw_body=raw_body)

    inbound = provider.parse(form=form, raw_body=raw_body)

    organization = await OrganizationRepository(session).get_by_support_email_address(
        session, inbound.to_address
    )
    return EmailWebhookRequest(organization=organization, email=inbound)


@dataclass(frozen=True, slots=True)
class WhatsAppWebhookRequest:
    """The verified inbound WhatsApp/Meta Cloud API webhook body, already
    parsed to JSON, plus its resolved organization.

    `organization` is `None` when no organization's `whatsapp_phone_number_id`
    matches the payload's `entry[].changes[].value.metadata.phone_number_id`
    — a business-level outcome (design ADR-9), NOT a signature failure: the
    caller (the webhook route) acks 200 for this case rather than treating
    it as an error.
    """

    organization: Organization | None
    payload: dict[str, Any]


def _extract_phone_number_id(payload: dict[str, Any]) -> str | None:
    """Read `entry[0].changes[0].value.metadata.phone_number_id` off a Meta
    Cloud API webhook payload — Meta's documented shape. Defensively
    returns `None` for any missing/malformed key rather than raising: a
    status-callback-only payload (no `messages`) can still carry this
    shape, but a genuinely malformed body must not crash the webhook."""
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        return change["value"]["metadata"]["phone_number_id"]
    except (KeyError, IndexError, TypeError):
        return None


async def verify_and_parse_whatsapp_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> WhatsAppWebhookRequest:
    """Verify an inbound `POST /api/v1/webhooks/whatsapp` request's
    `X-Hub-Signature-256` header, parse its JSON body, and resolve which
    organization it belongs to.

    Verification happens BEFORE any database statement (isolation
    invariant I1): the raw body is read straight off `request` (no DB
    access of its own), `verify_meta_signature` runs next, and only once
    that succeeds does the org lookup touch the session at all.
    `InvalidWebhookSignatureError` is deliberately left to propagate
    uncaught — `app.main`'s registered exception handler (already
    registered for the email half in PR2) maps it to 401.

    A `None` `settings.whatsapp_app_secret` (misconfigured) fails CLOSED —
    raises `InvalidWebhookSignatureError` rather than silently accepting
    every payload, mirroring `verify_and_parse_email_webhook`'s own
    Mailgun-signing-key-`None` behavior.
    """
    settings = get_settings()
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256", "")

    if settings.whatsapp_app_secret is None:
        raise InvalidWebhookSignatureError("WhatsApp app secret is not configured")

    verify_meta_signature(raw_body, signature_header, settings.whatsapp_app_secret)

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning("inbound WhatsApp webhook: malformed JSON body")
        payload = {}

    phone_number_id = _extract_phone_number_id(payload)
    organization = None
    if phone_number_id is not None:
        organization = await OrganizationRepository(session).get_by_whatsapp_phone_number_id(
            session, phone_number_id
        )
    return WhatsAppWebhookRequest(organization=organization, payload=payload)
