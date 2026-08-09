"""Outbound email delivery via SMTP (Mailpit in local/dev, per the
CONFIRMED Mailpit decision: no vendor email API in this stage).
"""

from email.message import EmailMessage

import aiosmtplib

from app.core.config import get_settings

_FROM_ADDRESS = "no-reply@real-estate-helpdesk.local"


async def send_invite_email(*, to: str, invite_link: str) -> None:
    """Send an account-invitation email containing `invite_link` to `to`.

    Connects to the configured SMTP relay (Mailpit locally) on every call;
    there is no persistent connection pooling at this stage.
    """
    settings = get_settings()

    message = EmailMessage()
    message["From"] = _FROM_ADDRESS
    message["To"] = to
    message["Subject"] = "You've been invited to Real Estate Helpdesk"
    message.set_content(
        "You have been invited to join Real Estate Helpdesk.\n\n"
        f"Accept your invitation here: {invite_link}\n\n"
        "This link expires in 48 hours."
    )

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
    )
