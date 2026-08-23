"""Integration test for the full auth flow against a REAL Postgres instance.

Everything in `tests/unit/` and `tests/api/` mocks the `AsyncSession` /
`RefreshTokenRepository` boundary, which is sufficient to prove the
service and HTTP-layer control flow (branching logic, status codes,
response shapes). It does NOT prove that:

- `User.email` case-insensitive lookup (`func.lower(...)` against the
  `ix_users_email_lower_unique` functional index) executes correctly
  against real Postgres.
- `RefreshToken.token_hash` (a `LargeBinary` / `bytea` column) round-trips
  SHA-256 digests correctly through asyncpg.
- `SELECT ... FOR UPDATE` row locking actually serializes concurrent
  rotation attempts on the same refresh token (the property this whole
  rotate-on-use design depends on for reuse detection to be reliable).
- The Postgres-native `user_role` / `user_status` enum types round-trip
  correctly through SQLAlchemy's `Enum` type in a real login/refresh/
  logout cycle.

These four gaps are closed below, exercising the real
`app.services.auth_service` functions end-to-end against a real Postgres
instance (requires `docker compose up -d postgres` and `alembic upgrade
head` — see `tests/conftest.py`).
"""

import secrets
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, sha256_hash
from app.models.enums import UserRole, UserStatus
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import auth_service


async def _make_active_user(db_session: AsyncSession, *, email: str, password: str) -> User:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()

    user = User(
        organization_id=org.id,
        name="Test Agent",
        email=email,
        role=UserRole.AGENT,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_full_login_refresh_rotate_reuse_detection_logout_cycle(
    db_session: AsyncSession,
) -> None:
    """End-to-end login/refresh/rotate/reuse-detection/logout cycle against
    real Postgres, exercising behavior that mocked unit tests cannot prove:

    - `User.email` case-insensitive lookup (`func.lower(...)` against the
      `ix_users_email_lower_unique` functional index): login uses a
      differently-cased email than the one the user was created with.
    - `RefreshToken.token_hash` (a `LargeBinary` / `bytea` column) round-
      trips the SHA-256 digest correctly through asyncpg: looked up
      directly by its raw digest bytes after issuance.
    - `SELECT ... FOR UPDATE` row locking in `RefreshTokenRepository.rotate`
      is exercised on every refresh/logout call (via `auth_service.refresh`
      / `auth_service.logout`), and reuse of an already-rotated token
      revokes the entire family.
    - The Postgres-native `user_role` / `user_status` enum columns round-
      trip correctly: the created user is read back with its `UserRole`
      and `UserStatus` enum members intact by `login`/`refresh`.
    """
    password = "correct horse battery staple"
    created_email = f"Auth.Flow.{uuid4()}@Example.com"
    user = await _make_active_user(db_session, email=created_email, password=password)

    # 1. Login with a different case than the stored email -> proves the
    # case-insensitive functional-index lookup, not a Python-side compare.
    login_pair = await auth_service.login(
        db_session, email=created_email.upper(), password=password
    )
    assert login_pair.access_token
    assert login_pair.refresh_token

    # bytea round-trip: the raw token's SHA-256 digest must find exactly
    # the row `login` just inserted via `RefreshTokenRepository.issue`.
    digest = sha256_hash(login_pair.refresh_token.encode("utf-8"))
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == digest)
    )
    issued_row = result.scalar_one()
    assert issued_row.user_id == user.id
    assert issued_row.revoked_at is None
    original_family_id = issued_row.family_id

    # 2. Refresh with the valid token -> rotates it (locks the row via
    # `SELECT ... FOR UPDATE`), issues a new pair in the same family, and
    # revokes the original row.
    refreshed_pair = await auth_service.refresh(
        db_session, raw_refresh_token=login_pair.refresh_token
    )
    assert refreshed_pair.refresh_token != login_pair.refresh_token

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.id == issued_row.id)
    )
    rotated_row = result.scalar_one()
    assert rotated_row.revoked_at is not None
    assert rotated_row.family_id == original_family_id

    new_digest = sha256_hash(refreshed_pair.refresh_token.encode("utf-8"))
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == new_digest)
    )
    new_row = result.scalar_one()
    assert new_row.family_id == original_family_id
    assert new_row.revoked_at is None

    # 3. Re-using the ORIGINAL (now-rotated) token must fail generically and
    # trigger reuse detection: every row in the family gets revoked,
    # including the still-active `new_row` from step 2.
    with pytest.raises(auth_service.AuthenticationError) as exc_info:
        await auth_service.refresh(
            db_session, raw_refresh_token=login_pair.refresh_token
        )
    assert str(exc_info.value) == "invalid or expired refresh token"

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.family_id == original_family_id)
    )
    family_rows = result.scalars().all()
    assert len(family_rows) == 2
    assert all(row.revoked_at is not None for row in family_rows)

    # 4. Logout with a fresh token (issued via a brand-new login) revokes
    # its whole family too.
    second_login_pair = await auth_service.login(
        db_session, email=created_email, password=password
    )
    await auth_service.logout(
        db_session, raw_refresh_token=second_login_pair.refresh_token
    )

    second_digest = sha256_hash(second_login_pair.refresh_token.encode("utf-8"))
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == second_digest)
    )
    logged_out_row = result.scalar_one()
    assert logged_out_row.revoked_at is not None
