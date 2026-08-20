"""Unit tests for `app.services.whatsapp_client` — the WhatsApp Cloud API
`send_text` HTTP primitive (stage 5 — multichannel, PR5 — WhatsApp worker +
outbound; design ADR-10/ADR-12).

No live network in this sandbox: `httpx.AsyncClient` is monkeypatched at the
module boundary with a fake async-context-manager class returning a canned
response, following the exact "mock at the module boundary" convention
`tests/unit/test_mailer_service.py` establishes for `aiosmtplib.send`.

`app.core.crypto.decrypt_secret` is this module's SOLE call site for
decrypting the org's WhatsApp access token (never re-implemented, never
logged) — see the module docstring of `app.core.crypto`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import get_settings
from app.services import whatsapp_client
from app.services.whatsapp_client import WhatsAppSendError, send_text


def _configure_encryption_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: _FakeCryptoSettings(key))
    return key


class _FakeCryptoSettings:
    def __init__(self, key: str) -> None:
        self.secret_encryption_key = key


def _encrypted_token(monkeypatch, plaintext: str) -> str:
    _configure_encryption_key(monkeypatch)
    return crypto.encrypt_secret(plaintext)


class _FakeResponse:
    def __init__(self, *, status_code: int, json_body: dict) -> None:
        self.status_code = status_code
        self._json_body = json_body

    def json(self) -> dict:
        return self._json_body


class _FakeAsyncClient:
    """Stands in for `httpx.AsyncClient` as an async context manager whose
    `post()` returns a pre-canned `_FakeResponse` — `captured` records the
    exact args/kwargs `send_text` invoked it with, for assertions."""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.post_calls: list[tuple[tuple, dict]] = []
        self._response = _FakeAsyncClient.next_response
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self._response


def _install_fake_client(monkeypatch, response: _FakeResponse) -> None:
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.next_response = response
    monkeypatch.setattr(whatsapp_client.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.asyncio
async def test_send_text_posts_expected_url_payload_and_bearer_token(monkeypatch) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "super-secret-wa-token")
    _install_fake_client(
        monkeypatch,
        _FakeResponse(status_code=200, json_body={"messages": [{"id": "wamid.SENT123"}]}),
    )

    result = await send_text(
        phone_number_id="1234567890",
        to="15550001111",
        body="Thanks, we're on it.",
        access_token_encrypted=token_encrypted,
    )

    assert result == {"messages": [{"id": "wamid.SENT123"}]}
    client = _FakeAsyncClient.instances[0]
    (call_args, call_kwargs) = client.post_calls[0]
    settings = get_settings()
    expected_url = (
        f"{settings.whatsapp_api_base_url}/{settings.whatsapp_api_version}"
        "/1234567890/messages"
    )
    assert call_args[0] == expected_url
    assert call_kwargs["json"] == {
        "messaging_product": "whatsapp",
        "to": "15550001111",
        "type": "text",
        "text": {"body": "Thanks, we're on it."},
    }
    assert call_kwargs["headers"]["Authorization"] == "Bearer super-secret-wa-token"


@pytest.mark.asyncio
async def test_send_text_uses_configured_send_timeout(monkeypatch) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "tok")
    _install_fake_client(
        monkeypatch, _FakeResponse(status_code=200, json_body={"messages": [{"id": "x"}]})
    )

    await send_text(
        phone_number_id="1234567890",
        to="15550001111",
        body="hi",
        access_token_encrypted=token_encrypted,
    )

    client = _FakeAsyncClient.instances[0]
    assert client.init_kwargs["timeout"] == get_settings().whatsapp_send_timeout_seconds


@pytest.mark.asyncio
async def test_send_text_decrypt_secret_is_the_sole_decryption_call_site(monkeypatch) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "tok")
    _install_fake_client(
        monkeypatch, _FakeResponse(status_code=200, json_body={"messages": [{"id": "x"}]})
    )
    spy = MagicMock(wraps=crypto.decrypt_secret)
    monkeypatch.setattr(whatsapp_client, "decrypt_secret", spy)

    await send_text(
        phone_number_id="1234567890",
        to="15550001111",
        body="hi",
        access_token_encrypted=token_encrypted,
    )

    spy.assert_called_once_with(token_encrypted)


@pytest.mark.asyncio
async def test_send_text_non_2xx_raises_whatsapp_send_error_with_meta_error_code(
    monkeypatch,
) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "tok")
    _install_fake_client(
        monkeypatch,
        _FakeResponse(
            status_code=400,
            json_body={
                "error": {
                    "message": "Message failed to send because more than 24 hours have "
                    "passed since the customer last replied to this number.",
                    "code": 131047,
                }
            },
        ),
    )

    with pytest.raises(WhatsAppSendError) as exc_info:
        await send_text(
            phone_number_id="1234567890",
            to="15550001111",
            body="hi",
            access_token_encrypted=token_encrypted,
        )

    assert exc_info.value.meta_error_code == 131047
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_send_text_non_2xx_without_parseable_error_body_still_raises(monkeypatch) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "tok")

    class _UnparseableResponse(_FakeResponse):
        def json(self) -> dict:
            raise ValueError("not json")

    _install_fake_client(monkeypatch, _UnparseableResponse(status_code=500, json_body={}))

    with pytest.raises(WhatsAppSendError) as exc_info:
        await send_text(
            phone_number_id="1234567890",
            to="15550001111",
            body="hi",
            access_token_encrypted=token_encrypted,
        )

    assert exc_info.value.meta_error_code is None
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_send_text_never_logs_the_decrypted_token(monkeypatch, caplog) -> None:
    token_encrypted = _encrypted_token(monkeypatch, "super-secret-wa-token")
    _install_fake_client(
        monkeypatch,
        _FakeResponse(status_code=400, json_body={"error": {"code": 131056}}),
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(WhatsAppSendError):
            await send_text(
                phone_number_id="1234567890",
                to="15550001111",
                body="hi",
                access_token_encrypted=token_encrypted,
            )

    for record in caplog.records:
        assert "super-secret-wa-token" not in record.getMessage()
        assert "Bearer" not in record.getMessage()
