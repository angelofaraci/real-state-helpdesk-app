"""Inbound-email webhook provider abstraction (stage 5 — multichannel, PR2
— email inbound).

`EmailWebhookProvider` is the narrow swap point the design calls for: every
route/service downstream of `get_email_provider()` speaks only in terms of
this protocol (`verify` + `parse`) and the provider-agnostic `InboundEmail`
shape, never Mailgun's specific form-field names. Only `MailgunProvider`
(the currently-supported provider, per `settings.email_webhook_provider`)
knows those.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.webhook_signature import InvalidWebhookSignatureError, verify_mailgun_signature

# Single-use replay-token cache TTL (seconds). Comfortably longer than
# `settings.email_webhook_max_age_seconds` (5 min default) so a token can
# never be replayed even at the very edge of the freshness window, while
# still expiring instead of growing Redis memory unbounded.
_REPLAY_CACHE_TTL_SECONDS = 600


@dataclass(frozen=True)
class InboundEmail:
    """A provider-agnostic inbound email, already verified and parsed."""

    provider_message_id: str
    in_reply_to: str | None
    references: list[str]
    from_address: str
    from_display_name: str | None
    to_address: str
    subject: str
    text_body: str


class EmailWebhookProvider(Protocol):
    async def verify(self, *, headers: dict, form: dict, raw_body: bytes) -> None:
        """Raise `InvalidWebhookSignatureError` if this request is not an
        authentic, fresh, first-seen delivery from the provider. Must be
        called (and must succeed) before any database statement runs
        (isolation invariant I1)."""
        ...

    def parse(self, *, form: dict, raw_body: bytes) -> InboundEmail:
        """Build the provider-agnostic `InboundEmail` from an already
        `verify()`-passed request. Pure — no I/O."""
        ...


class MailgunProvider:
    """`EmailWebhookProvider` for Mailgun's inbound-route webhook.

    https://documentation.mailgun.com/en/latest/user_manual.html#webhooks —
    Mailgun POSTs `timestamp`/`token`/`signature` as ordinary form fields
    alongside the message fields (not as a header), so `verify` reads them
    straight out of `form`.
    """

    def __init__(self, *, settings: Settings, redis: object) -> None:
        self._settings = settings
        self._redis = redis

    async def verify(self, *, headers: dict, form: dict, raw_body: bytes) -> None:
        signing_key = self._settings.mailgun_signing_key
        if signing_key is None:
            # Fail closed: an unconfigured signing key must reject every
            # request, never silently accept one (same convention as
            # `app.core.crypto`'s `SecretEncryptionUnavailableError`).
            raise InvalidWebhookSignatureError("mailgun_signing_key is not configured")

        timestamp = form.get("timestamp")
        token = form.get("token")
        signature = form.get("signature")
        if not timestamp or not token or not signature:
            raise InvalidWebhookSignatureError("missing Mailgun signature fields")

        verify_mailgun_signature(timestamp, token, signature, signing_key)

        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise InvalidWebhookSignatureError("malformed Mailgun timestamp") from exc

        now = int(time.time())
        if abs(now - timestamp_value) > self._settings.email_webhook_max_age_seconds:
            raise InvalidWebhookSignatureError(
                "Mailgun webhook timestamp outside the freshness window"
            )

        replay_key = f"webhook:mg:{token}"
        was_first_seen = await self._redis.set(
            replay_key, "1", nx=True, ex=_REPLAY_CACHE_TTL_SECONDS
        )
        if not was_first_seen:
            raise InvalidWebhookSignatureError("Mailgun webhook token replay detected")

    def parse(self, *, form: dict, raw_body: bytes) -> InboundEmail:
        from_display_name, from_address = parseaddr(form.get("From", ""))

        # `recipient` is Mailgun's own field for the exact address that
        # matched the inbound route (a single address, always present on a
        # route-matched delivery); `To` is the raw header, which may carry
        # multiple/differently-cased addresses — prefer `recipient`, fall
        # back to parsing `To` only if it is somehow absent.
        to_address = form.get("recipient") or parseaddr(form.get("To", ""))[1]

        references_raw = form.get("References", "")
        references = references_raw.split() if references_raw else []

        # Mailgun sends both `stripped-text` (reply-quote-stripped, the
        # cleaner choice for a ticket message body) and `body-plain` (the
        # full raw plain-text body). Prefer `stripped-text`; fall back to
        # `body-plain` since some inbound-route configurations omit
        # `stripped-*` fields.
        text_body = form.get("stripped-text") or form.get("body-plain") or ""

        return InboundEmail(
            provider_message_id=form.get("Message-Id", ""),
            in_reply_to=form.get("In-Reply-To") or None,
            references=references,
            from_address=from_address,
            from_display_name=from_display_name or None,
            to_address=to_address,
            subject=form.get("Subject", ""),
            text_body=text_body,
        )


def get_email_provider(*, redis: object) -> EmailWebhookProvider:
    """Return the configured `EmailWebhookProvider`
    (`settings.email_webhook_provider`) — the narrow swap point for adding
    a second inbound-email provider later. Only `"mailgun"` is implemented
    today; any other configured value fails closed with `NotImplementedError`
    rather than silently falling back to Mailgun."""
    settings = get_settings()
    provider_name = settings.email_webhook_provider
    if provider_name == "mailgun":
        return MailgunProvider(settings=settings, redis=redis)
    raise NotImplementedError(f"unsupported email_webhook_provider: {provider_name!r}")
