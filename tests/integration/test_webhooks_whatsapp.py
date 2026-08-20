"""API-level tests for `GET`/`POST /api/v1/webhooks/whatsapp` (stage 5 —
multichannel, PR4 — WhatsApp webhook). No live Postgres is available in
this sandbox, so `whatsapp_ingestion.ingest_inbound_whatsapp_message` and
the arq-enqueue helper are monkeypatched at the module boundary and
`get_session`/`verify_and_parse_whatsapp_webhook`/`get_redis` are overridden
with inert stand-ins, following the exact pattern
`tests/integration/test_webhooks_email.py` already establishes.

Design ADR-9: every business-level outcome (no matching org, a cross-org
sender conflict, a replayed message, zero messages in the payload) is
ack'd with 200 — only an invalid/missing `X-Hub-Signature-256` signature is
ever rejected (401).

Isolation invariants:
- I1: signature failure -> no DB access at all (proven at the dependency
  level, `tests/integration/test_deps_webhooks_whatsapp.py`; re-asserted
  here at the route boundary via the 401 status code).
- I2: a webhook payload for org A must never read/write org B data — org
  resolution happens entirely inside the (mocked-here) dependency, keyed
  only by the verified `phone_number_id`, so this module asserts the
  ingestion call always receives EXACTLY the dependency-resolved org, never
  something derived from payload content.
- I5: a replayed `wamid` produces no duplicate processing/enqueue.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps_chat
from app.api.deps_webhooks import WhatsAppWebhookRequest, verify_and_parse_whatsapp_webhook
from app.api.v1 import webhooks_whatsapp as webhooks_whatsapp_module
from app.core.config import get_settings
from app.core.session import get_session
from app.core.webhook_signature import InvalidWebhookSignatureError
from app.main import app
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, TicketChannel
from app.models.organization import Organization
from app.services import whatsapp_ingestion
from app.services.channel_identity import CrossOrgSenderConflict
from app.services.whatsapp_ingestion import WhatsAppIngestionOutcome, WhatsAppIngestionResult


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    app.dependency_overrides[deps_chat.get_redis] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _org(**overrides) -> Organization:
    defaults = dict(id=uuid4(), name="Landlord Co", whatsapp_phone_number_id="1234567890")
    defaults.update(overrides)
    return Organization(**defaults)


def _chat_session(*, organization_id, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        status=ChatSessionStatus.ACTIVE,
        channel=TicketChannel.WHATSAPP,
        channel_metadata=None,
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _value(*, phone_number_id="1234567890", wa_id="15550001111", wamid="wamid.ABC", messages=None):
    if messages is None:
        messages = [{"id": wamid, "type": "text", "text": {"body": "hi there"}}]
    return {
        "metadata": {"phone_number_id": phone_number_id},
        "contacts": [{"wa_id": wa_id, "profile": {"name": "Jane"}}],
        "messages": messages,
    }


def _payload(*, value=None) -> dict:
    return {"entry": [{"changes": [{"value": value or _value()}]}]}


def _override_webhook(webhook: WhatsAppWebhookRequest) -> None:
    app.dependency_overrides[verify_and_parse_whatsapp_webhook] = lambda: webhook


# ---------------------------------------------------------------------------
# GET verification handshake
# ---------------------------------------------------------------------------


def test_verification_handshake_echoes_challenge_on_matching_token(client, monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "shared-secret")
    get_settings.cache_clear()

    response = client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "shared-secret", "hub.challenge": "12345"},
    )

    assert response.status_code == 200
    assert response.text == "12345"
    get_settings.cache_clear()


def test_verification_handshake_rejects_mismatched_token(client, monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "shared-secret")
    get_settings.cache_clear()

    response = client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )

    assert response.status_code == 403
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# POST inbound webhook
# ---------------------------------------------------------------------------


def test_invalid_signature_maps_to_401(client) -> None:
    def _raise_invalid_signature():
        raise InvalidWebhookSignatureError("bad signature")

    app.dependency_overrides[verify_and_parse_whatsapp_webhook] = _raise_invalid_signature

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 401


def test_unmatched_organization_acks_200_without_ingesting(client, monkeypatch) -> None:
    _override_webhook(WhatsAppWebhookRequest(organization=None, payload=_payload()))
    mocked_ingest = AsyncMock()
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mocked_ingest.assert_not_called()


def test_zero_messages_payload_acks_200_as_a_noop(client, monkeypatch) -> None:
    org = _org()
    payload = _payload(value=_value(messages=[]))
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=payload))
    mocked_ingest = AsyncMock()
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    mocked_ingest.assert_not_called()


def test_new_message_enqueues_arq_job(client, monkeypatch) -> None:
    org = _org()
    payload = _payload()
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=payload))
    chat_session = _chat_session(organization_id=org.id)
    mocked_ingest = AsyncMock(
        return_value=WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.BOT_REPLY_QUEUED,
            chat_session=chat_session,
            wamid="wamid.ABC",
            text_body="hi there",
        )
    )
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(
        webhooks_whatsapp_module, "enqueue_whatsapp_message_processing", mocked_enqueue
    )

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    mocked_ingest.assert_awaited_once()
    ingest_kwargs = mocked_ingest.call_args.kwargs
    assert ingest_kwargs["organization"] is org
    assert ingest_kwargs["phone_number_id"] == "1234567890"
    assert ingest_kwargs["contact"] == {"wa_id": "15550001111", "profile": {"name": "Jane"}}
    mocked_enqueue.assert_awaited_once_with(chat_session.id, "wamid.ABC", "hi there")


def test_escalated_outcome_does_not_enqueue(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=_payload()))
    chat_session = _chat_session(organization_id=org.id, status=ChatSessionStatus.ESCALATED)
    mocked_ingest = AsyncMock(
        return_value=WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.APPENDED_TO_ESCALATED_TICKET,
            chat_session=chat_session,
            wamid="wamid.ABC",
            text_body="hi there",
        )
    )
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(
        webhooks_whatsapp_module, "enqueue_whatsapp_message_processing", mocked_enqueue
    )

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    mocked_enqueue.assert_not_called()


def test_replayed_message_acks_200_without_enqueuing(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=_payload()))
    chat_session = _chat_session(organization_id=org.id)
    mocked_ingest = AsyncMock(
        return_value=WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.REPLAYED,
            chat_session=chat_session,
            wamid="wamid.ABC",
        )
    )
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(
        webhooks_whatsapp_module, "enqueue_whatsapp_message_processing", mocked_enqueue
    )

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    mocked_enqueue.assert_not_called()


def test_cross_org_sender_conflict_acks_200_and_continues(client, monkeypatch) -> None:
    org = _org()
    payload = _payload(
        value=_value(
            messages=[
                {"id": "wamid.1", "type": "text", "text": {"body": "first"}},
                {"id": "wamid.2", "type": "text", "text": {"body": "second"}},
            ]
        )
    )
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=payload))
    conflict = CrossOrgSenderConflict(
        organization_id=org.id, existing_user_organization_id=uuid4(), channel=TicketChannel.WHATSAPP
    )
    chat_session = _chat_session(organization_id=org.id)
    mocked_ingest = AsyncMock(
        side_effect=[
            conflict,
            WhatsAppIngestionResult(
                outcome=WhatsAppIngestionOutcome.BOT_REPLY_QUEUED,
                chat_session=chat_session,
                wamid="wamid.2",
                text_body="second",
            ),
        ]
    )
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(
        webhooks_whatsapp_module, "enqueue_whatsapp_message_processing", mocked_enqueue
    )

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert mocked_ingest.await_count == 2
    mocked_enqueue.assert_awaited_once_with(chat_session.id, "wamid.2", "second")


def test_rate_limited_outcome_does_not_enqueue(client, monkeypatch) -> None:
    org = _org()
    _override_webhook(WhatsAppWebhookRequest(organization=org, payload=_payload()))
    chat_session = _chat_session(organization_id=org.id)
    mocked_ingest = AsyncMock(
        return_value=WhatsAppIngestionResult(
            outcome=WhatsAppIngestionOutcome.RATE_LIMITED,
            chat_session=chat_session,
            wamid="wamid.ABC",
        )
    )
    monkeypatch.setattr(whatsapp_ingestion, "ingest_inbound_whatsapp_message", mocked_ingest)
    mocked_enqueue = AsyncMock()
    monkeypatch.setattr(
        webhooks_whatsapp_module, "enqueue_whatsapp_message_processing", mocked_enqueue
    )

    response = client.post("/api/v1/webhooks/whatsapp", json={})

    assert response.status_code == 200
    mocked_enqueue.assert_not_called()
