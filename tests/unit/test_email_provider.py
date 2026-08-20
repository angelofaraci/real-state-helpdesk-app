"""Unit tests for `app.services.email_provider` — the Mailgun inbound-email
webhook provider (stage 5 — multichannel, PR2 — email inbound).

Follows `tests/unit/test_chat_rate_limit.py`'s `FakeRedis` convention (no
real Redis package/service is available in this sandbox): a minimal
in-memory stand-in implementing just the `set(key, value, nx=, ex=)`
coroutine `MailgunProvider.verify` needs for its single-use replay-token
cache.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.core.config import Settings
from app.core.webhook_signature import InvalidWebhookSignatureError
from app.services.email_provider import (
    InboundEmail,
    MailgunProvider,
    get_email_provider,
)


class FakeRedis:
    """Minimal in-memory stand-in for the subset of `redis.asyncio.Redis`
    `MailgunProvider.verify` uses: `SET key value NX EX seconds`."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True


def _mailgun_signature(timestamp: str, token: str, signing_key: str) -> str:
    return hmac.new(
        signing_key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


def _settings(**overrides) -> Settings:
    defaults = dict(mailgun_signing_key="mailgun-signing-key", email_webhook_max_age_seconds=300)
    defaults.update(overrides)
    return Settings(**defaults)


def _valid_form(*, signing_key: str, timestamp: str | None = None, token: str = "token-1") -> dict:
    ts = timestamp or str(int(time.time()))
    return {
        "timestamp": ts,
        "token": token,
        "signature": _mailgun_signature(ts, token, signing_key),
    }


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_accepts_a_valid_fresh_first_seen_signature() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = _valid_form(signing_key=settings.mailgun_signing_key)

    await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_rejects_an_invalid_signature() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = _valid_form(signing_key=settings.mailgun_signing_key)
    form["signature"] = "0" * 64

    with pytest.raises(InvalidWebhookSignatureError):
        await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_rejects_a_stale_timestamp() -> None:
    settings = _settings(email_webhook_max_age_seconds=300)
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    stale_timestamp = str(int(time.time()) - 1000)
    form = _valid_form(signing_key=settings.mailgun_signing_key, timestamp=stale_timestamp)

    with pytest.raises(InvalidWebhookSignatureError):
        await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_accepts_a_timestamp_within_the_freshness_window() -> None:
    settings = _settings(email_webhook_max_age_seconds=300)
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    recent_timestamp = str(int(time.time()) - 100)
    form = _valid_form(signing_key=settings.mailgun_signing_key, timestamp=recent_timestamp)

    await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_rejects_a_replayed_token() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = _valid_form(signing_key=settings.mailgun_signing_key, token="replayed-token")

    await provider.verify(headers={}, form=form, raw_body=b"")

    with pytest.raises(InvalidWebhookSignatureError):
        await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_allows_two_different_tokens() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form_a = _valid_form(signing_key=settings.mailgun_signing_key, token="token-a")
    form_b = _valid_form(signing_key=settings.mailgun_signing_key, token="token-b")

    await provider.verify(headers={}, form=form_a, raw_body=b"")
    await provider.verify(headers={}, form=form_b, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_fails_closed_when_signing_key_is_not_configured() -> None:
    settings = _settings(mailgun_signing_key=None)
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = _valid_form(signing_key="whatever-key")

    with pytest.raises(InvalidWebhookSignatureError):
        await provider.verify(headers={}, form=form, raw_body=b"")


@pytest.mark.asyncio
async def test_verify_rejects_missing_signature_fields() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())

    with pytest.raises(InvalidWebhookSignatureError):
        await provider.verify(headers={}, form={}, raw_body=b"")


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_builds_inbound_email_from_mailgun_form_fields() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = {
        "Message-Id": "<inbound-1@mailgun.example>",
        "In-Reply-To": "<inbound-0@mailgun.example>",
        "References": "<inbound-a@mailgun.example> <inbound-b@mailgun.example>",
        "From": "Jane Tenant <jane@example.com>",
        "To": "support@landlord.example",
        "recipient": "support@landlord.example",
        "Subject": "Leaking faucet",
        "stripped-text": "It is still leaking.",
    }

    result = provider.parse(form=form, raw_body=b"")

    assert result == InboundEmail(
        provider_message_id="<inbound-1@mailgun.example>",
        in_reply_to="<inbound-0@mailgun.example>",
        references=["<inbound-a@mailgun.example>", "<inbound-b@mailgun.example>"],
        from_address="jane@example.com",
        from_display_name="Jane Tenant",
        to_address="support@landlord.example",
        subject="Leaking faucet",
        text_body="It is still leaking.",
    )


def test_parse_falls_back_to_body_plain_when_stripped_text_absent() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = {
        "Message-Id": "<inbound-1@mailgun.example>",
        "From": "jane@example.com",
        "To": "support@landlord.example",
        "Subject": "Leaking faucet",
        "body-plain": "Full raw body.",
    }

    result = provider.parse(form=form, raw_body=b"")

    assert result.text_body == "Full raw body."


def test_parse_defaults_in_reply_to_and_references_when_absent() -> None:
    settings = _settings()
    provider = MailgunProvider(settings=settings, redis=FakeRedis())
    form = {
        "Message-Id": "<inbound-1@mailgun.example>",
        "From": "jane@example.com",
        "To": "support@landlord.example",
        "Subject": "New issue",
        "stripped-text": "Hello.",
    }

    result = provider.parse(form=form, raw_body=b"")

    assert result.in_reply_to is None
    assert result.references == []


# ---------------------------------------------------------------------------
# get_email_provider
# ---------------------------------------------------------------------------


def test_get_email_provider_returns_mailgun_provider_by_default() -> None:
    provider = get_email_provider(redis=FakeRedis())

    assert isinstance(provider, MailgunProvider)


def test_get_email_provider_raises_for_an_unsupported_provider(monkeypatch) -> None:
    from app.services import email_provider

    monkeypatch.setattr(
        email_provider,
        "get_settings",
        lambda: Settings(email_webhook_provider="sendgrid"),
    )

    with pytest.raises(NotImplementedError):
        get_email_provider(redis=FakeRedis())
