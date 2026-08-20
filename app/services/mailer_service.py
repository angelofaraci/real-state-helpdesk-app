"""Outbound email delivery via SMTP (Mailpit in local/dev, per the
CONFIRMED Mailpit decision: no vendor email API in this stage).

`send_message` is the shared raw-SMTP-send primitive (stage 5 — multichannel,
PR3 — email outbound): both `send_invite_email` below and
`app.workers.email.send_ticket_email_reply` build their own stdlib
`email.message.EmailMessage` and hand it to `send_message`, rather than each
calling `aiosmtplib.send` independently.
"""

from email.message import EmailMessage

import aiosmtplib

from app.core.config import get_settings

_FROM_ADDRESS = "no-reply@real-estate-helpdesk.local"


async def send_message(message: EmailMessage) -> None:
    """Send an already-built `EmailMessage` over the configured SMTP relay
    (Mailpit locally). Connects fresh on every call; there is no persistent
    connection pooling at this stage."""
    settings = get_settings()
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
    )


async def send_invite_email(*, to: str, invite_link: str) -> None:
    """Send an account-invitation email containing `invite_link` to `to`."""
    message = EmailMessage()
    message["From"] = _FROM_ADDRESS
    message["To"] = to
    message["Subject"] = "You've been invited to Real Estate Helpdesk"
    message.set_content(
        "You have been invited to join Real Estate Helpdesk.\n\n"
        f"Accept your invitation here: {invite_link}\n\n"
        "This link expires in 48 hours."
    )

    await send_message(message)
