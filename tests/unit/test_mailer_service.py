"""Unit tests for `app.services.mailer_service`.

No live Mailpit is available in this sandbox, so `aiosmtplib.send` is
mocked at the module boundary. These tests verify the outbound message is
addressed and shaped correctly (recipient, host/port, invite link in the
body) — not real SMTP delivery. See the CONFIRMED Mailpit decision: a real
Docker Compose stack is required to verify actual delivery.
"""

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
