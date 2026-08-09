"""Unit tests for `app.core.jwt` — access token encode/decode."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest

from app.core.jwt import decode_access_token, encode_access_token
from app.core.jwt import InvalidAccessTokenError


def test_encode_access_token_roundtrips_through_decode() -> None:
    user_id = uuid4()
    org_id = uuid4()
    token = encode_access_token(sub=user_id, org=org_id, role="agent")

    claims = decode_access_token(token)

    assert claims["sub"] == str(user_id)
    assert claims["org"] == str(org_id)
    assert claims["role"] == "agent"
    assert claims["typ"] == "access"
    assert "jti" in claims
    assert "iat" in claims
    assert "exp" in claims


def test_encode_access_token_allows_null_org_for_admin() -> None:
    token = encode_access_token(sub=uuid4(), org=None, role="admin")
    claims = decode_access_token(token)
    assert claims["org"] is None


def test_encode_access_token_default_expiry_is_twenty_minutes() -> None:
    token = encode_access_token(sub=uuid4(), org=uuid4(), role="tenant")
    claims = decode_access_token(token)
    delta = claims["exp"] - claims["iat"]
    assert delta == 20 * 60


def test_encode_access_token_respects_custom_expiry() -> None:
    token = encode_access_token(
        sub=uuid4(), org=uuid4(), role="tenant", expires_in=timedelta(minutes=5)
    )
    claims = decode_access_token(token)
    assert claims["exp"] - claims["iat"] == 5 * 60


def test_decode_access_token_rejects_expired_token() -> None:
    token = encode_access_token(
        sub=uuid4(), org=uuid4(), role="tenant", expires_in=timedelta(seconds=-1)
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token)


def test_decode_access_token_rejects_tampered_signature() -> None:
    token = encode_access_token(sub=uuid4(), org=uuid4(), role="tenant")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(tampered)


def test_decode_access_token_rejects_wrong_typ_claim() -> None:
    # A refresh-typed (or otherwise mis-typed) JWT must never be usable
    # as an access token, even if signed with the correct secret.
    from app.core.config import get_settings

    settings = get_settings()
    now = datetime.now(UTC)
    bogus = pyjwt.encode(
        {
            "sub": str(uuid4()),
            "org": None,
            "role": "admin",
            "jti": str(uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "typ": "refresh",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(bogus)
