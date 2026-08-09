"""Unit tests for `app.core.security` — password hashing and token helpers.

These are pure-Python tests: no database connection, no FastAPI app.
"""

from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    needs_rehash,
    new_opaque_token,
    sha256_hash,
    verify_password,
)


def test_hash_password_produces_argon2id_hash() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_correct_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password(hashed, "correct-horse-battery-staple") is True


def test_verify_password_rejects_incorrect_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password(hashed, "wrong-password") is False


def test_verify_password_rejects_malformed_hash_without_raising() -> None:
    assert verify_password("not-a-real-hash", "whatever") is False


def test_dummy_password_hash_is_a_valid_argon2id_hash() -> None:
    # Used for timing-equalization on unknown-email login attempts.
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert verify_password(DUMMY_PASSWORD_HASH, "anything") is False


def test_needs_rehash_is_false_for_freshly_hashed_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert needs_rehash(hashed) is False


def test_sha256_hash_is_deterministic_and_32_bytes() -> None:
    raw = b"some-opaque-refresh-token-value"
    digest = sha256_hash(raw)
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert sha256_hash(raw) == digest


def test_new_opaque_token_returns_url_safe_unique_strings() -> None:
    token_a = new_opaque_token()
    token_b = new_opaque_token()
    assert token_a != token_b
    assert len(token_a) >= 32
    assert all(c not in token_a for c in ("+", "/", "="))
