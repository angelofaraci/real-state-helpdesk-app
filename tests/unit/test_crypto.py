"""Unit tests for `app.core.crypto` — symmetric encryption for secrets at
rest (stage 5 — multichannel, e.g. the WhatsApp access token).

`get_settings()` is monkeypatched at the module boundary (same convention
as `tests/unit/test_mailer_service.py`'s `mailer_service.aiosmtplib.send`)
so these tests never depend on a real `secret_encryption_key` env var.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.crypto import (
    SecretDecryptionError,
    SecretEncryptionUnavailableError,
    decrypt_secret,
    encrypt_secret,
    secret_encryption_available,
)


def _settings_with_key(key: str | None) -> SimpleNamespace:
    return SimpleNamespace(secret_encryption_key=key)


def test_encrypt_secret_then_decrypt_secret_roundtrips(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key))

    token = encrypt_secret("super-secret-whatsapp-token")

    assert token != "super-secret-whatsapp-token"
    assert decrypt_secret(token) == "super-secret-whatsapp-token"


def test_encrypt_secret_raises_when_no_key_configured(monkeypatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(None))

    with pytest.raises(SecretEncryptionUnavailableError):
        encrypt_secret("plaintext")


def test_decrypt_secret_raises_when_no_key_configured(monkeypatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(None))

    with pytest.raises(SecretEncryptionUnavailableError):
        decrypt_secret("some-token")


def test_encrypt_secret_raises_when_key_is_blank_string(monkeypatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(""))

    with pytest.raises(SecretEncryptionUnavailableError):
        encrypt_secret("plaintext")


def test_secret_encryption_available_reflects_configured_key(monkeypatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(None))
    assert secret_encryption_available() is False

    monkeypatch.setattr(
        crypto, "get_settings", lambda: _settings_with_key(Fernet.generate_key().decode())
    )
    assert secret_encryption_available() is True


def test_decrypt_secret_supports_key_rotation_via_comma_separated_keys(monkeypatch) -> None:
    """`MultiFernet` rotation contract: a ciphertext produced with the OLD
    key must still decrypt once the NEW key is listed first, as long as the
    old key is still present (second) in the comma-separated list."""
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(old_key))
    token_encrypted_with_old_key = encrypt_secret("rotate-me")

    monkeypatch.setattr(
        crypto, "get_settings", lambda: _settings_with_key(f"{new_key},{old_key}")
    )
    assert decrypt_secret(token_encrypted_with_old_key) == "rotate-me"


def test_decrypt_secret_raises_secret_decryption_error_on_tampered_token(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key))
    token = encrypt_secret("some-plaintext")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(SecretDecryptionError):
        decrypt_secret(tampered)


def test_decrypt_secret_raises_secret_decryption_error_when_key_does_not_match(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        crypto, "get_settings", lambda: _settings_with_key(Fernet.generate_key().decode())
    )
    token = encrypt_secret("some-plaintext")

    monkeypatch.setattr(
        crypto, "get_settings", lambda: _settings_with_key(Fernet.generate_key().decode())
    )
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(token)
