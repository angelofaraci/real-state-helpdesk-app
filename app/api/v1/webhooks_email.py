"""`POST /api/v1/webhooks/email` — inbound email webhook (stage 5 —
multichannel, PR2 — email inbound).

Design ADR-9: every BUSINESS-level outcome acks 200 — no matching
organization, a cross-org sender conflict, a matched reply, or a brand-new
ticket. Only an invalid/missing provider signature is ever rejected, and
that happens entirely inside `verify_and_parse_email_webhook`'s own
`Depends` — `InvalidWebhookSignatureError` propagates uncaught to `app.
main`'s registered handler (401), never a local `try`/`except` here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_webhooks import EmailWebhookRequest, verify_and_parse_email_webhook
from app.core.session import get_session
from app.models.enums import ClassificationStatus
from app.services import email_ingestion
from app.services.channel_identity import CrossOrgSenderConflict
from app.services.email_ingestion import EmailIngestionOutcome
from app.workers.classification import enqueue_classification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_ACK = {"status": "ok"}


@router.post("/email", status_code=status.HTTP_200_OK)
async def receive_email_webhook(
    background_tasks: BackgroundTasks,
    webhook: EmailWebhookRequest = Depends(verify_and_parse_email_webhook),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Ingest one verified inbound email. A brand-new ticket
    (`classification_status="pending"`, always true for an email-originated
    ticket — category/urgency are never supplied on this path) enqueues a
    `classify_ticket` job via `BackgroundTasks`, mirroring `app.api.v1.
    tickets.create_ticket`'s own hook exactly."""
    if webhook.organization is None:
        # No org's `support_email_address` matches the inbound `to`
        # address — a business-level outcome (ADR-9), not an error.
        logger.warning(
            "inbound email webhook: no organization matches the inbound support address"
        )
        return _ACK

    try:
        result = await email_ingestion.ingest_inbound_email(
            session, organization=webhook.organization, inbound=webhook.email
        )
    except CrossOrgSenderConflict as exc:
        # Isolation invariant I4: never the address itself in the log line.
        logger.warning(
            "inbound email webhook: cross-org sender conflict "
            "organization_id=%s existing_user_organization_id=%s",
            exc.organization_id,
            exc.existing_user_organization_id,
        )
        return _ACK

    if (
        result.outcome == EmailIngestionOutcome.TICKET_CREATED
        and result.ticket.classification_status == ClassificationStatus.PENDING
    ):
        background_tasks.add_task(
            enqueue_classification, result.ticket.id, result.ticket.organization_id
        )

    return _ACK
