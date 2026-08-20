"""WhatsApp Cloud API client — the outbound `send_text` HTTP primitive
(stage 5 — multichannel, PR5 — WhatsApp worker + outbound; design
ADR-10/ADR-12).

`send_text` is the SOLE call site in this whole codebase for decrypting an
org's WhatsApp access token (`app.core.crypto.decrypt_secret`) — the
ciphertext is handed in and the plaintext token never leaves this function
(it is used exactly once, to build the `Authorization: Bearer <token>`
header, and is never logged, returned, or stored).

On a non-2xx response, `WhatsAppSendError` carries Meta's own error `code`
(parsed from the response body's `{"error": {"code": ..., "message": ...}}`
shape, when present) so `app.workers.whatsapp.send_whatsapp_reply` can
detect code `131047` (the customer-service-window/re-engagement error)
specifically and route to the deferred-reply path instead of retrying.

Logging never includes the decrypted token or the raw `Authorization`
header — only non-secret identifiers (`phone_number_id`, the HTTP status,
Meta's error code). This module has no `organization_id`/`wamid` of its
own to log (its signature carries neither) — the caller
(`app.workers.whatsapp`) is responsible for logging those alongside
whatever this module raises.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.crypto import decrypt_secret

logger = logging.getLogger(__name__)


class WhatsAppSendError(Exception):
    """Raised when the WhatsApp Cloud API responds with a non-2xx status to
    `send_text`. `meta_error_code` is Meta's own `error.code` from the
    response body, when parseable — `None` otherwise (e.g. a non-JSON body,
    or a JSON body with no `error.code`)."""

    def __init__(self, *, status_code: int, meta_error_code: int | None) -> None:
        self.status_code = status_code
        self.meta_error_code = meta_error_code
        super().__init__(
            f"WhatsApp Cloud API send failed: http_status={status_code} "
            f"meta_error_code={meta_error_code}"
        )


def _extract_meta_error_code(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, int) else None


async def send_text(
    *, phone_number_id: str, to: str, body: str, access_token_encrypted: str
) -> dict[str, Any]:
    """Send a plain-text WhatsApp message via the Cloud API's `/messages`
    endpoint, returning the parsed JSON response (which includes
    `messages[0].id` — the sent message's `wamid` — on success).

    Raises `WhatsAppSendError` on any non-2xx response.
    """
    settings = get_settings()
    token = decrypt_secret(access_token_encrypted)

    url = (
        f"{settings.whatsapp_api_base_url}/{settings.whatsapp_api_version}"
        f"/{phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=settings.whatsapp_send_timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)

    if response.status_code >= 400:
        meta_error_code = _extract_meta_error_code(response)
        logger.warning(
            "WhatsApp send_text failed phone_number_id=%s http_status=%s meta_error_code=%s",
            phone_number_id,
            response.status_code,
            meta_error_code,
        )
        raise WhatsAppSendError(status_code=response.status_code, meta_error_code=meta_error_code)

    return response.json()
