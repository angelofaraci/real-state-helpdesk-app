"""Symmetric encryption for secrets at rest (stage 5 — multichannel, e.g.
the WhatsApp Cloud API access token stored on `organizations`).

Wraps `cryptography.fernet.MultiFernet` over one or more Fernet keys parsed
from `settings.secret_encryption_key` (comma-separated urlsafe-base64
32-byte keys). The FIRST key always encrypts; ALL keys can decrypt — this
is what makes key rotation possible: deploy with the new key listed first
and the old key second, then drop the old key once every ciphertext has
been re-encrypted.

Fails closed: if no key is configured, encryption/decryption is refused
outright (`SecretEncryptionUnavailableError`) rather than silently falling
back to a plaintext no-op.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class SecretEncryptionUnavailableError(Exception):
    """Raised when no `secret_encryption_key` is configured, so a secret
    can be neither encrypted nor decrypted."""


class SecretDecryptionError(Exception):
    """Raised when a ciphertext cannot be decrypted with any of the
    currently configured keys (wrong/rotated-out key, or tampering)."""


def _multi_fernet() -> MultiFernet:
    settings = get_settings()
    raw_key = settings.secret_encryption_key
    if not raw_key:
        raise SecretEncryptionUnavailableError(
            "no secret_encryption_key configured; cannot encrypt/decrypt secrets"
        )
    keys = [Fernet(part.strip().encode()) for part in raw_key.split(",") if part.strip()]
    if not keys:
        raise SecretEncryptionUnavailableError(
            "no secret_encryption_key configured; cannot encrypt/decrypt secrets"
        )
    return MultiFernet(keys)


def secret_encryption_available() -> bool:
    """Whether a usable `secret_encryption_key` is currently configured."""
    return bool(get_settings().secret_encryption_key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt `plaintext` with the first configured key, returning a
    urlsafe-base64 token string.

    Raises `SecretEncryptionUnavailableError` if no key is configured.
    """
    fernet = _multi_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt `token` (as produced by `encrypt_secret`) back to plaintext,
    trying every configured key in order.

    Raises `SecretEncryptionUnavailableError` if no key is configured, or
    `SecretDecryptionError` if `token` cannot be decrypted with any
    configured key (wrong/rotated-out key, or tampering).
    """
    fernet = _multi_fernet()
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError("could not decrypt secret") from exc
