"""Unit tests for `app.core.chat_token` — chat-session JWTs (stage 4 —
chatbot).

These tokens are signed with the SAME secret as the real access token
(`settings.jwt_secret` — see D1 in the design), so the only thing that
prevents a chat-session token being replayed as a real access token (or
vice versa) is the `typ` claim. `decode_access_token` already rejects any
`typ != "access"` token (see `app/core/jwt.py`), so reusing that EXISTING
function against a chat-session token is the actual non-replayability
proof, not a hand-rolled assertion.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.chat_token import (
    InvalidChatSessionTokenError,
    decode_chat_session_token,
    encode_chat_session_token,
)
from app.core.config import get_settings
from app.core.jwt import InvalidAccessTokenError, decode_access_token


def test_encode_chat_session_token_roundtrips_through_decode() -> None:
    session_id = uuid4()
    org_id = uuid4()
    user_id = uuid4()

    token = encode_chat_session_token(
        session_id=session_id, organization_id=org_id, user_id=user_id
    )
    claims = decode_chat_session_token(token)

    assert claims.sid == str(session_id)
    assert claims.org == str(org_id)
    assert claims.uid == str(user_id)
    assert claims.typ == "chat_session"
    assert claims.jti
    assert claims.iat
    assert claims.exp


def test_encode_chat_session_token_allows_null_user_id_for_anonymous() -> None:
    token = encode_chat_session_token(
        session_id=uuid4(), organization_id=uuid4(), user_id=None
    )
    claims = decode_chat_session_token(token)
    assert claims.uid is None


def test_encode_chat_session_token_uses_configured_ttl() -> None:
    settings = get_settings()
    token = encode_chat_session_token(
        session_id=uuid4(), organization_id=uuid4(), user_id=None
    )
    claims = decode_chat_session_token(token)
    delta = claims.exp - claims.iat
    assert delta == settings.chat_session_token_ttl_minutes * 60


def test_decode_chat_session_token_rejects_expired_token() -> None:
    from datetime import timedelta

    token = encode_chat_session_token(
        session_id=uuid4(),
        organization_id=uuid4(),
        user_id=None,
        expires_in=timedelta(seconds=-1),
    )
    with pytest.raises(InvalidChatSessionTokenError):
        decode_chat_session_token(token)


def test_decode_chat_session_token_rejects_tampered_signature() -> None:
    token = encode_chat_session_token(
        session_id=uuid4(), organization_id=uuid4(), user_id=None
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidChatSessionTokenError):
        decode_chat_session_token(tampered)


def test_decode_access_token_rejects_a_chat_session_token() -> None:
    """The EXISTING real-auth decoder must reject a chat-session token,
    proving a chat-session token can never be replayed against a real
    authenticated route, even though both are signed with the same secret.
    """
    token = encode_chat_session_token(
        session_id=uuid4(), organization_id=uuid4(), user_id=uuid4()
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token)


def test_decode_chat_session_token_rejects_a_real_access_token() -> None:
    """Symmetric proof: a real access token must never be usable as a
    chat-session token either."""
    from app.core.jwt import encode_access_token

    token = encode_access_token(sub=uuid4(), org=uuid4(), role="agent")
    with pytest.raises(InvalidChatSessionTokenError):
        decode_chat_session_token(token)
