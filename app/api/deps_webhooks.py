"""FastAPI dependencies for inbound channel webhooks (stage 5 —
multichannel).

Email half only (PR2). The WhatsApp half (Meta Cloud API webhook
verification + org resolution by `whatsapp_phone_number_id`) is added by a
later PR (PR4) as a sibling dependency in this same module.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_chat import get_redis
from app.core.session import get_session
from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository
from app.services.email_provider import EmailWebhookProvider, InboundEmail, get_email_provider


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
