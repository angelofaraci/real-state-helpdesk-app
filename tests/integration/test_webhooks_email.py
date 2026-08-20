"""API-level tests for `POST /api/v1/webhooks/email` (stage 5 —
multichannel, PR2 — email inbound). No live Postgres is available in this
sandbox, so `email_ingestion`/`enqueue_classification` are monkeypatched at
the module boundary and `get_session`/`verify_and_parse_email_webhook` are
overridden with inert stand-ins, following the pattern established by
`tests/api/test_tickets_endpoints.py`.

Design ADR-9: every business-level outcome (no matching org, a cross-org
sender conflict, a matched reply, a brand-new ticket) is ack'd with 200 —
only an invalid/missing provider signature is ever rejected (401).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps_webhooks import EmailWebhookRequest, verify_and_parse_email_webhook
from app.api.v1 import webhooks_email as webhooks_email_module
from app.core.session import get_session
from app.core.webhook_signature import InvalidWebhookSignatureError
from app.main import app
from app.models.enums import ClassificationStatus, TicketChannel, TicketStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.services import email_ingestion
from app.services.channel_identity import CrossOrgSenderConflict
from app.services.email_ingestion import EmailIngestionOutcome, EmailIngestionResult
from app.services.email_provider import InboundEmail


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _org(**overrides) -> Organization:
    defaults = dict(id=uuid4(), name="Landlord Co", support_email_address="support@landlord.example")
    defaults.update(overrides)
    return Organization(**defaults)


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


def _ticket(*, classification_status=ClassificationStatus.PENDING, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        user_id=uuid4(),
        title="Leaking faucet",
        channel=TicketChannel.EMAIL,
        status=TicketStatus.OPEN,
        classification_status=classification_status,
        channel_metadata=None,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _override_webhook(webhook: EmailWebhookRequest) -> None:
    app.dependency_overrides[verify_and_parse_email_webhook] = lambda: webhook


def test_invalid_signature_maps_to_401(client) -> None:
    def _raise_invalid_signature():
        raise InvalidWebhookSignatureError("bad signature")

    app.dependency_overrides[verify_and_parse_email_webhook] = _raise_invalid_signature

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 401


def test_unmatched_organization_acks_200_without_ingesting(client, monkeypatch) -> None:
    inbound = _inbound(to_address="unknown@nowhere.example")
    _override_webhook(EmailWebhookRequest(organization=None, email=inbound))
    mocked_ingest = AsyncMock()
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", mocked_ingest)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mocked_ingest.assert_not_called()


def test_known_thread_reply_appends_message_and_does_not_enqueue_classification(
    client, monkeypatch
) -> None:
    org = _org()
    inbound = _inbound()
    _override_webhook(EmailWebhookRequest(organization=org, email=inbound))
    ticket = _ticket(organization_id=org.id)
    mocked_ingest = AsyncMock(
        return_value=EmailIngestionResult(outcome=EmailIngestionOutcome.MESSAGE_APPENDED, ticket=ticket)
    )
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_email_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mocked_ingest.assert_awaited_once()
    call_kwargs = mocked_ingest.call_args.kwargs
    assert call_kwargs["organization"] is org
    assert call_kwargs["inbound"] is inbound
    mocked_enqueue.assert_not_called()


def test_replayed_message_acks_200_without_enqueuing_classification(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(EmailWebhookRequest(organization=org, email=_inbound()))
    ticket = _ticket(organization_id=org.id)
    mocked_ingest = AsyncMock(
        return_value=EmailIngestionResult(outcome=EmailIngestionOutcome.REPLAYED, ticket=ticket)
    )
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_email_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    mocked_enqueue.assert_not_called()


def test_unmatched_email_creates_ticket_and_enqueues_classification(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(EmailWebhookRequest(organization=org, email=_inbound()))
    ticket = _ticket(organization_id=org.id, classification_status=ClassificationStatus.PENDING)
    mocked_ingest = AsyncMock(
        return_value=EmailIngestionResult(outcome=EmailIngestionOutcome.TICKET_CREATED, ticket=ticket)
    )
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_email_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    mocked_enqueue.assert_awaited_once_with(ticket.id, ticket.organization_id)


def test_ticket_created_with_taxonomy_already_classified_does_not_enqueue(
    client, monkeypatch
) -> None:
    """Defensive branch coverage: a `TICKET_CREATED` outcome whose ticket
    is somehow already classified must not enqueue a redundant job —
    mirrors `app.api.v1.tickets.create_ticket`'s own
    `classification_status == PENDING` gate."""
    org = _org()
    _override_webhook(EmailWebhookRequest(organization=org, email=_inbound()))
    ticket = _ticket(organization_id=org.id, classification_status=ClassificationStatus.CLASSIFIED)
    mocked_ingest = AsyncMock(
        return_value=EmailIngestionResult(outcome=EmailIngestionOutcome.TICKET_CREATED, ticket=ticket)
    )
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_email_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    mocked_enqueue.assert_not_called()


def test_cross_org_sender_conflict_acks_200_and_does_not_enqueue(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(EmailWebhookRequest(organization=org, email=_inbound()))
    conflict = CrossOrgSenderConflict(
        organization_id=org.id, existing_user_organization_id=uuid4(), channel=TicketChannel.EMAIL
    )
    monkeypatch.setattr(email_ingestion, "ingest_inbound_email", AsyncMock(side_effect=conflict))
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_email_module, "enqueue_classification", mocked_enqueue)

    response = client.post("/api/v1/webhooks/email", data={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mocked_enqueue.assert_not_called()
