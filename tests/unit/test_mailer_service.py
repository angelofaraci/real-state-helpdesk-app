"""Unit tests for `app.services.mailer_service`.

No live Mailpit is available in this sandbox, so `aiosmtplib.send` is
mocked at the module boundary. These tests verify the outbound message is
addressed and shaped correctly (recipient, host/port, invite link in the
body) — not real SMTP delivery. See the CONFIRMED Mailpit decision: a real
Docker Compose stack is required to verify actual delivery.

Stage 5 (PR3 — email outbound) extracts a `send_message(EmailMessage)`
primitive: the raw SMTP-sending call, shared between `send_invite_email`
and `app.workers.email.send_ticket_email_reply`. `send_invite_email` is now
a thin builder over it — its own external behavior (recipient, subject,
body) must stay byte-for-byte identical, which the two original tests above
already pin down.
"""

from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest

from app.services import mailer_service


@pytest.mark.asyncio
async def test_send_invite_email_calls_aiosmtplib_send_with_expected_recipient_and_host(
    monkeypatch,
) -> None:
    mock_send = AsyncMock(return_value=({}, "OK"))
    monkeypatch.setattr(mailer_service.aiosmtplib, "send", mock_send)

    await mailer_service.send_invite_email(
        to="jane@example.com", invite_link="http://localhost:3000/accept-invite?token=abc123"
    )

    mock_send.assert_awaited_once()
    message = mock_send.call_args.args[0]
    kwargs = mock_send.call_args.kwargs
    assert message["To"] == "jane@example.com"
    assert kwargs["hostname"] == mailer_service.get_settings().smtp_host
    assert kwargs["port"] == mailer_service.get_settings().smtp_port


@pytest.mark.asyncio
async def test_send_invite_email_body_contains_the_invite_link(monkeypatch) -> None:
    mock_send = AsyncMock(return_value=({}, "OK"))
    monkeypatch.setattr(mailer_service.aiosmtplib, "send", mock_send)
    invite_link = "http://localhost:3000/accept-invite?token=super-secret-token"

    await mailer_service.send_invite_email(to="jane@example.com", invite_link=invite_link)

    message = mock_send.call_args.args[0]
    assert invite_link in message.get_content()


# ---------------------------------------------------------------------------
# Stage 5 (PR3) — the extracted `send_message` primitive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_calls_aiosmtplib_send_with_configured_host_and_port(
    monkeypatch,
) -> None:
    mock_send = AsyncMock(return_value=({}, "OK"))
    monkeypatch.setattr(mailer_service.aiosmtplib, "send", mock_send)

    message = EmailMessage()
    message["From"] = "support@landlord.example"
    message["To"] = "jane@example.com"
    message["Subject"] = "Re: Leaking faucet"
    message.set_content("Thanks, we're on it.")

    await mailer_service.send_message(message)

    mock_send.assert_awaited_once_with(
        message,
        hostname=mailer_service.get_settings().smtp_host,
        port=mailer_service.get_settings().smtp_port,
    )


@pytest.mark.asyncio
async def test_send_invite_email_delegates_to_send_message(monkeypatch) -> None:
    """`send_invite_email` builds a stdlib `EmailMessage` and hands it to
    `send_message` — the shared SMTP-sending primitive — rather than calling
    `aiosmtplib.send` itself."""
    mock_send_message = AsyncMock()
    monkeypatch.setattr(mailer_service, "send_message", mock_send_message)
    invite_link = "http://localhost:3000/accept-invite?token=abc123"

    await mailer_service.send_invite_email(to="jane@example.com", invite_link=invite_link)

    mock_send_message.assert_awaited_once()
    (built_message,), _ = mock_send_message.call_args
    assert isinstance(built_message, EmailMessage)
    assert built_message["To"] == "jane@example.com"
    assert invite_link in built_message.get_content()
