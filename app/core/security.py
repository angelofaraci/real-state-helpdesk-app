"""Password hashing and opaque-token helpers.

Per the approved design: argon2id is used ONLY for password hashing.
Opaque refresh tokens are hashed with SHA-256 before storage (they are
already high-entropy random values, so a slow KDF adds no security
benefit there and would be needlessly expensive to verify on every
refresh/rotate).
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

# A valid argon2id hash of a constant, never-used value. Used to run a
# dummy `verify_password` call when a login attempt targets an email that
# does not exist, so unknown-email and wrong-password responses take a
# comparable amount of time (no user-enumeration timing signal).
DUMMY_PASSWORD_HASH = _hasher.hash("dummy-password-for-timing-equalization")


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with argon2id."""
    return _hasher.hash(plain_password)


def verify_password(hashed_password: str, plain_password: str) -> bool:
    """Verify a plaintext password against an argon2id hash.

    Returns `False` for a wrong password AND for a malformed/foreign hash,
    instead of raising, so callers can use it directly in auth control
    flow without a try/except at every call site.
    """
    try:
        return _hasher.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(hashed_password: str) -> bool:
    """Return True if `hashed_password` was hashed with outdated parameters.

    Callers should re-hash and persist the password on successful login
    when this returns True (check-and-upgrade path).
    """
    return _hasher.check_needs_rehash(hashed_password)


def sha256_hash(raw: bytes) -> bytes:
    """Return the SHA-256 digest of `raw`, used to hash opaque tokens at rest."""
    return hashlib.sha256(raw).digest()


def new_opaque_token() -> str:
    """Generate a new URL-safe opaque token backed by 32 random bytes."""
    return secrets.token_urlsafe(32)
