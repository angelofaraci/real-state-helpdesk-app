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

No live Postgres is reachable in this sandbox (same constraint noted in
Work Unit 0 — `localhost:5432` connection attempts time out). This test
is intentionally skipped rather than faking Postgres-specific behavior
with SQLite. It documents the exact scope that remains unverified against
a real database and should be run in CI / a real dev environment before
this work unit is considered fully proven.
"""

import pytest


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in this sandbox. Run manually or in CI with "
    "`docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_full_login_refresh_rotate_reuse_detection_logout_cycle() -> None:
    """Placeholder for the real end-to-end flow against Postgres:

    1. Create an active user with a real password hash.
    2. POST /api/v1/auth/login -> 200, token pair.
    3. POST /api/v1/auth/refresh with the refresh token -> 200, NEW pair.
    4. Re-POST /api/v1/auth/refresh with the ORIGINAL (now-rotated) token
       -> 401, and confirm every token in the family is revoked
       (reuse detection).
    5. POST /api/v1/auth/logout with a fresh token -> 204, and confirm the
       family is revoked.
    """
    raise NotImplementedError("run against a live Postgres instance")
