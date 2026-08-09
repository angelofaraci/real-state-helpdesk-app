"""Access-token JWT encode/decode helpers.

Claims: `sub` (user id), `org` (organization id, nullable for admins),
`role`, `jti` (unique token id), `iat`, `exp`, `typ` ("access" always, so
a refresh token can never be replayed as an access token even though both
are signed with the same secret).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt as pyjwt

from app.core.config import get_settings

DEFAULT_ACCESS_TOKEN_EXPIRY = timedelta(minutes=20)


class InvalidAccessTokenError(Exception):
    """Raised when a token fails signature, expiry, or claim-shape checks."""


def encode_access_token(
    *,
    sub: UUID,
    org: UUID | None,
    role: str,
    expires_in: timedelta = DEFAULT_ACCESS_TOKEN_EXPIRY,
) -> str:
    """Encode a signed HS256 access token for the given principal."""
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": str(sub),
        "org": str(org) if org is not None else None,
        "role": role,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "typ": "access",
    }
    return pyjwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token, returning its claims.

    Raises `InvalidAccessTokenError` on any signature, expiry, or
    claim-shape problem (never leaks the underlying PyJWT exception type
    to callers).
    """
    settings = get_settings()
    try:
        claims = pyjwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except pyjwt.PyJWTError as exc:
        raise InvalidAccessTokenError("invalid or expired access token") from exc

    if claims.get("typ") != "access":
        raise InvalidAccessTokenError("token is not an access token")

    return claims
