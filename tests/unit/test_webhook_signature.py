"""Unit tests for `app.core.webhook_signature` — inbound webhook signature
verification for the stage-5 multichannel channels (Meta/WhatsApp Cloud API
and Mailgun email). Pure functions, no DB/I-O, so no fixtures needed.
"""

import hashlib
import hmac

import pytest

from app.core.webhook_signature import (
    InvalidWebhookSignatureError,
    verify_mailgun_signature,
    verify_meta_signature,
)


def _meta_signature(raw_body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _mailgun_signature(timestamp: str, token: str, signing_key: str) -> str:
    return hmac.new(
        signing_key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


def test_verify_meta_signature_accepts_a_valid_signature() -> None:
    app_secret = "meta-app-secret"
    raw_body = b'{"object":"whatsapp_business_account"}'
    signature_header = _meta_signature(raw_body, app_secret)

    verify_meta_signature(raw_body, signature_header, app_secret)


def test_verify_meta_signature_rejects_an_invalid_signature() -> None:
    app_secret = "meta-app-secret"
    raw_body = b'{"object":"whatsapp_business_account"}'

    with pytest.raises(InvalidWebhookSignatureError):
        verify_meta_signature(raw_body, "sha256=" + "0" * 64, app_secret)


def test_verify_meta_signature_rejects_a_tampered_body() -> None:
    app_secret = "meta-app-secret"
    raw_body = b'{"object":"whatsapp_business_account"}'
    signature_header = _meta_signature(raw_body, app_secret)

    with pytest.raises(InvalidWebhookSignatureError):
        verify_meta_signature(raw_body + b"tampered", signature_header, app_secret)


def test_verify_meta_signature_rejects_malformed_header_without_prefix() -> None:
    app_secret = "meta-app-secret"
    raw_body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    with pytest.raises(InvalidWebhookSignatureError):
        verify_meta_signature(raw_body, digest, app_secret)


def test_verify_meta_signature_uses_constant_time_compare(monkeypatch) -> None:
    """Proves `hmac.compare_digest` (not `==`) gates the comparison."""
    from app.core import webhook_signature

    calls: list[tuple[str, str]] = []
    real_compare_digest = hmac.compare_digest

    def _spy_compare_digest(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(webhook_signature.hmac, "compare_digest", _spy_compare_digest)

    app_secret = "meta-app-secret"
    raw_body = b"payload"
    signature_header = _meta_signature(raw_body, app_secret)
    verify_meta_signature(raw_body, signature_header, app_secret)

    assert len(calls) == 1


def test_verify_mailgun_signature_accepts_a_valid_signature() -> None:
    signing_key = "mailgun-signing-key"
    timestamp = "1719000000"
    token = "abcd1234token"
    signature = _mailgun_signature(timestamp, token, signing_key)

    verify_mailgun_signature(timestamp, token, signature, signing_key)


def test_verify_mailgun_signature_rejects_an_invalid_signature() -> None:
    signing_key = "mailgun-signing-key"
    timestamp = "1719000000"
    token = "abcd1234token"

    with pytest.raises(InvalidWebhookSignatureError):
        verify_mailgun_signature(timestamp, token, "0" * 64, signing_key)


def test_verify_mailgun_signature_rejects_a_tampered_timestamp() -> None:
    signing_key = "mailgun-signing-key"
    timestamp = "1719000000"
    token = "abcd1234token"
    signature = _mailgun_signature(timestamp, token, signing_key)

    with pytest.raises(InvalidWebhookSignatureError):
        verify_mailgun_signature("1719000001", token, signature, signing_key)


def test_verify_mailgun_signature_rejects_wrong_signing_key() -> None:
    timestamp = "1719000000"
    token = "abcd1234token"
    signature = _mailgun_signature(timestamp, token, "correct-key")

    with pytest.raises(InvalidWebhookSignatureError):
        verify_mailgun_signature(timestamp, token, signature, "wrong-key")
