"""Tests for `app.api.deps_webhooks`'s WhatsApp/Meta-webhook dependency
(stage 5 — multichannel, PR4 — WhatsApp webhook).

`AsyncSession` is faked at the dependency's own function boundary, the
exact pattern `tests/integration/test_deps_webhooks_email.py` already
establishes for the email half — this proves the dependency's control flow
(verify-before-any-DB-read, then org resolution) without a live Postgres.

Isolation invariant I1: an invalid/missing `X-Hub-Signature-256` signature
must be rejected with ZERO database statements — the org lookup must never
even be attempted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.api.deps_webhooks import verify_and_parse_whatsapp_webhook
from app.core.webhook_signature import InvalidWebhookSignatureError
from app.models.organization import Organization

_APP_SECRET = "test-app-secret"


class _FakeRequest:
    def __init__(self, *, raw_body: bytes, headers: dict | None = None) -> None:
        self._raw_body = raw_body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._raw_body


def _session_returning(value) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = value
    session.execute.return_value = execute_result
    return session


def _signed_request(payload: dict, *, secret: str = _APP_SECRET) -> _FakeRequest:
    raw_body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return _FakeRequest(
        raw_body=raw_body, headers={"X-Hub-Signature-256": f"sha256={signature}"}
    )


def _payload(*, phone_number_id: str = "1234567890") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": "15550001111", "profile": {"name": "Jane"}}],
                            "messages": [
                                {
                                    "id": "wamid.ABC123",
                                    "type": "text",
                                    "text": {"body": "hi there"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_with_zero_db_statements(monkeypatch) -> None:
    """Isolation invariant I1: the org lookup must never run when
    signature verification fails."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("WHATSAPP_APP_SECRET", _APP_SECRET)
    get_settings.cache_clear()

    session = _session_returning(None)
    request = _FakeRequest(
        raw_body=json.dumps(_payload()).encode(),
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )

    with pytest.raises(InvalidWebhookSignatureError):
        await verify_and_parse_whatsapp_webhook(request=request, session=session)

    session.execute.assert_not_called()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_missing_app_secret_fails_closed_with_zero_db_statements(monkeypatch) -> None:
    """A misconfigured (`None`) `whatsapp_app_secret` must reject every
    payload rather than silently accepting it."""
    from app.core.config import get_settings

    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    get_settings.cache_clear()

    session = _session_returning(None)
    request = _signed_request(_payload())

    with pytest.raises(InvalidWebhookSignatureError):
        await verify_and_parse_whatsapp_webhook(request=request, session=session)

    session.execute.assert_not_called()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_valid_signature_resolves_the_matching_organization(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("WHATSAPP_APP_SECRET", _APP_SECRET)
    get_settings.cache_clear()

    org = Organization(id=uuid4(), name="Landlord Co", whatsapp_phone_number_id="1234567890")
    session = _session_returning(org)
    request = _signed_request(_payload(phone_number_id="1234567890"))

    result = await verify_and_parse_whatsapp_webhook(request=request, session=session)

    assert result.organization is org
    assert result.payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] == (
        "1234567890"
    )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unmatched_phone_number_id_returns_no_organization_without_raising(
    monkeypatch,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("WHATSAPP_APP_SECRET", _APP_SECRET)
    get_settings.cache_clear()

    session = _session_returning(None)
    request = _signed_request(_payload(phone_number_id="unknown"))

    result = await verify_and_parse_whatsapp_webhook(request=request, session=session)

    assert result.organization is None
    get_settings.cache_clear()
