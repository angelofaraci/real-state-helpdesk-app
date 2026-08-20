"""Inbound webhook signature verification (stage 5 — multichannel).

Pure functions, no DB/I-O: each takes the raw material a webhook route
already has on hand (the exact raw request body/header, or Mailgun's
`timestamp`/`token`/`signature` form fields) and either returns silently
(valid) or raises `InvalidWebhookSignatureError` (invalid) — never a bool,
so a caller cannot accidentally ignore the return value and proceed as if
the signature had been checked.

Both comparisons use `hmac.compare_digest` — never `==` — so verification
takes constant time regardless of where the strings first differ, closing
a timing side-channel that would otherwise let an attacker guess the
correct signature one byte at a time.
"""

from __future__ import annotations

import hashlib
import hmac

_META_SIGNATURE_PREFIX = "sha256="


class InvalidWebhookSignatureError(Exception):
    """Raised when an inbound webhook's signature does not match the
    expected HMAC for its raw payload."""


def verify_meta_signature(raw_body: bytes, signature_header: str, app_secret: str) -> None:
    """Verify a Meta/WhatsApp Cloud API webhook's `X-Hub-Signature-256`
    header against `raw_body`, HMAC-SHA256'd with `app_secret`.

    `signature_header` must be in Meta's documented `sha256=<hex>` form.
    Raises `InvalidWebhookSignatureError` on any mismatch or malformed
    header.
    """
    if not signature_header.startswith(_META_SIGNATURE_PREFIX):
        raise InvalidWebhookSignatureError(
            "malformed X-Hub-Signature-256 header: missing 'sha256=' prefix"
        )
    provided_hex = signature_header[len(_META_SIGNATURE_PREFIX) :]
    expected_hex = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hex, provided_hex):
        raise InvalidWebhookSignatureError("Meta webhook signature does not match")


def verify_mailgun_signature(
    timestamp: str, token: str, signature: str, signing_key: str
) -> None:
    """Verify a Mailgun webhook's `signature` field against
    `HMAC-SHA256(timestamp + token)`, keyed with `signing_key`.

    Raises `InvalidWebhookSignatureError` on any mismatch.
    """
    expected_hex = hmac.new(
        signing_key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hex, signature):
        raise InvalidWebhookSignatureError("Mailgun webhook signature does not match")
