"""Chat-session JWT encode/decode helpers (stage 4 — chatbot).

Claims: `sid` (chat session id), `org` (organization id), `uid` (user id,
nullable for an anonymous widget visitor), `jti` (unique token id), `iat`,
`exp`, `typ` (always `"chat_session"`).

Deliberately signed with the SAME secret as the real access token
(`settings.jwt_secret` — see `app.core.jwt`): the `typ` claim, not a
separate secret, is what prevents a chat-session token from ever being
replayed against a real authenticated route, or a real access token from
being replayed as a chat-session token. `app.core.jwt.decode_access_token`
already rejects any `typ != "access"` token, so this mirrors that same
one-secret / discriminated-`typ` design rather than inventing a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt as pyjwt

from app.core.config import get_settings

CHAT_SESSION_TOKEN_TYP = "chat_session"


class InvalidChatSessionTokenError(Exception):
    """Raised when a token fails signature, expiry, or claim-shape checks."""


@dataclass(frozen=True, slots=True)
class ChatTokenClaims:
    """Decoded, validated claims of a chat-session token."""

    sid: str
    org: str
    uid: str | None
    jti: str
    iat: int
    exp: int
    typ: str


def encode_chat_session_token(
    *,
    session_id: UUID,
    organization_id: UUID,
    user_id: UUID | None,
    expires_in: timedelta | None = None,
) -> str:
    """Encode a signed HS256 chat-session token for `session_id`."""
    settings = get_settings()
    if expires_in is None:
        expires_in = timedelta(minutes=settings.chat_session_token_ttl_minutes)
    now = datetime.now(UTC)
    claims = {
        "sid": str(session_id),
        "org": str(organization_id),
        "uid": str(user_id) if user_id is not None else None,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "typ": CHAT_SESSION_TOKEN_TYP,
    }
    return pyjwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_chat_session_token(token: str) -> ChatTokenClaims:
    """Decode and validate a chat-session token, returning its claims.

    Raises `InvalidChatSessionTokenError` on any signature, expiry, or
    claim-shape problem (never leaks the underlying PyJWT exception type
    to callers).
    """
    settings = get_settings()
    try:
        claims = pyjwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except pyjwt.PyJWTError as exc:
        raise InvalidChatSessionTokenError(
            "invalid or expired chat session token"
        ) from exc

    if claims.get("typ") != CHAT_SESSION_TOKEN_TYP:
        raise InvalidChatSessionTokenError("token is not a chat session token")

    try:
        return ChatTokenClaims(
            sid=claims["sid"],
            org=claims["org"],
            uid=claims.get("uid"),
            jti=claims["jti"],
            iat=claims["iat"],
            exp=claims["exp"],
            typ=claims["typ"],
        )
    except KeyError as exc:
        raise InvalidChatSessionTokenError("token is missing required claims") from exc
