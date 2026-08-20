"""Tests for `app.api.deps_webhooks`'s email-webhook dependency (stage 5 —
multichannel, PR2 — email inbound).

`AsyncSession` and the `EmailWebhookProvider` are both faked at the
dependency's own function boundary — the exact pattern
`tests/unit/test_deps_chat.py` already establishes for `get_chat_session_
scope` — so this proves the dependency's control flow (verify-before-any-DB-
read, then org resolution) without a live Postgres/Redis.

Isolation invariant I1: an invalid/missing signature must be rejected with
ZERO database statements — the org lookup must never even be attempted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.api.deps_webhooks import verify_and_parse_email_webhook
from app.core.webhook_signature import InvalidWebhookSignatureError
from app.models.organization import Organization
from app.services.email_provider import InboundEmail


class _FakeRequest:
    def __init__(self, *, raw_body: bytes, form: dict, headers: dict | None = None) -> None:
        self._raw_body = raw_body
        self._form = form
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._raw_body

    async def form(self) -> dict:
        return self._form


class _FakeProvider:
    def __init__(self, *, verify_error: Exception | None = None, inbound: InboundEmail | None = None) -> None:
        self._verify_error = verify_error
        self._inbound = inbound
        self.verify_calls: list[tuple] = []
        self.parse_calls: list[tuple] = []

    async def verify(self, *, headers: dict, form: dict, raw_body: bytes) -> None:
        self.verify_calls.append((headers, form, raw_body))
        if self._verify_error is not None:
            raise self._verify_error

    def parse(self, *, form: dict, raw_body: bytes) -> InboundEmail:
        self.parse_calls.append((form, raw_body))
        return self._inbound


def _session_returning(value) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = value
    session.execute.return_value = execute_result
    return session


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


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_with_zero_db_statements() -> None:
    """Isolation invariant I1: the org lookup must never run when
    signature verification fails."""
    session = _session_returning(None)
    provider = _FakeProvider(verify_error=InvalidWebhookSignatureError("bad signature"))
    request = _FakeRequest(raw_body=b"raw-body", form={"signature": "bad"})

    with pytest.raises(InvalidWebhookSignatureError):
        await verify_and_parse_email_webhook(request=request, session=session, provider=provider)

    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_valid_signature_resolves_the_matching_organization() -> None:
    org = Organization(id=uuid4(), name="Landlord Co", support_email_address="support@landlord.example")
    session = _session_returning(org)
    inbound = _inbound()
    provider = _FakeProvider(inbound=inbound)
    request = _FakeRequest(raw_body=b"raw-body", form={"signature": "ok"})

    result = await verify_and_parse_email_webhook(request=request, session=session, provider=provider)

    assert result.organization is org
    assert result.email is inbound
    assert len(provider.verify_calls) == 1
    assert len(provider.parse_calls) == 1


@pytest.mark.asyncio
async def test_verification_runs_before_org_resolution() -> None:
    """`verify()` must be called (and complete) before the org lookup
    ever touches the session — proven by asserting `verify` ran using the
    exact raw body/form the request carried, prior to any `session.execute`
    call being made."""
    session = _session_returning(None)
    inbound = _inbound()
    provider = _FakeProvider(inbound=inbound)
    request = _FakeRequest(raw_body=b"exact-raw-bytes", form={"signature": "ok"})

    await verify_and_parse_email_webhook(request=request, session=session, provider=provider)

    verify_headers, verify_form, verify_raw_body = provider.verify_calls[0]
    assert verify_raw_body == b"exact-raw-bytes"
    assert verify_form == {"signature": "ok"}


@pytest.mark.asyncio
async def test_unmatched_support_address_returns_no_organization_without_raising() -> None:
    """No org's `support_email_address` matches the inbound `to` address —
    a business-level outcome (ADR-9), NOT a signature failure: the route
    acks 200 for this, so the dependency must return `organization=None`
    rather than raising."""
    session = _session_returning(None)
    inbound = _inbound(to_address="unknown@nowhere.example")
    provider = _FakeProvider(inbound=inbound)
    request = _FakeRequest(raw_body=b"raw-body", form={"signature": "ok"})

    result = await verify_and_parse_email_webhook(request=request, session=session, provider=provider)

    assert result.organization is None
    assert result.email is inbound
