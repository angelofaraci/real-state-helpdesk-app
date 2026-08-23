"""Integration tests for invite-token revocation-on-reissue against a REAL
Postgres instance (stage 8 devops, PR2).

`tests/unit/test_invite_token_repository.py` proves `InviteTokenRepository`
builds the right ORM statements against a mocked `AsyncSession` (including
`test_revoke_unused_for_user_executes_update_scoped_to_user_unused_unrevoked`,
which compiles the `revoke_unused_for_user` UPDATE and asserts it is scoped
to `user_id`, `used_at IS NULL`, and `revoked_at IS NULL` — the exact
scoping the "concurrent reissue and consume: exactly one outcome succeeds"
invariant from the spec depends on for DB-level atomicity). It does NOT
prove that:

- Reissuing an invite via `app.services.user_service.reissue_invite`
  actually leaves the prior row in `invite_tokens` (not deleted) with
  `revoked_at` set and `used_at` still NULL, while the newly issued row has
  `revoked_at IS NULL`.
- A revoked token's raw value is rejected by
  `app.services.auth_service.accept_invite` with the exact same generic
  failure used for an expired or already-used token (no differentiating
  message/behavior).

These two gaps are closed below, exercising the real service functions
end-to-end (not just the repository) against a real Postgres instance.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole, UserStatus
from app.models.invite_token import InviteToken
from app.models.organization import Organization
from app.models.user import User
from app.repositories.invite_token_repository import InviteTokenRepository
from app.services import auth_service, user_service


async def _make_org_and_admin(db_session: AsyncSession) -> tuple[Organization, User]:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()

    admin = User(
        organization_id=org.id,
        name="Test Admin",
        email=f"{uuid4()}@example.com",
        role=UserRole.ADMIN,
        password_hash="not-a-real-hash",
        status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()

    return org, admin


async def _make_pending_user(db_session: AsyncSession, org: Organization) -> User:
    user = User(
        organization_id=org.id,
        name="Pending User",
        email=f"{uuid4()}@example.com",
        role=UserRole.AGENT,
        password_hash=None,
        status=UserStatus.PENDING,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_reissuing_an_invite_revokes_the_prior_token_row_without_deleting_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reissue_invite` must UPDATE the prior unused invite row's
    `revoked_at` (not DELETE it, and not touch `used_at`), and the newly
    issued token must have `revoked_at IS NULL`."""
    org, admin = await _make_org_and_admin(db_session)
    user = await _make_pending_user(db_session, org)

    prior_repo = InviteTokenRepository(db_session)
    await prior_repo.issue(user.id)

    # Look up the prior row directly by user_id (there is exactly one
    # outstanding invite for this user at this point).
    result = await db_session.execute(
        select(InviteToken).where(InviteToken.user_id == user.id)
    )
    prior_row = result.scalar_one()
    prior_row_id = prior_row.id

    captured_invite_link: dict[str, str] = {}

    async def _fake_send_invite_email(*, to: str, invite_link: str) -> None:
        captured_invite_link["invite_link"] = invite_link

    monkeypatch.setattr(user_service, "send_invite_email", _fake_send_invite_email)

    await user_service.reissue_invite(db_session, admin=admin, user_id=user.id)
    await db_session.flush()

    # Prior row: still present, revoked_at set, used_at still NULL.
    result = await db_session.execute(
        select(InviteToken).where(InviteToken.id == prior_row_id)
    )
    prior_row_after = result.scalar_one()
    assert prior_row_after is not None
    assert prior_row_after.revoked_at is not None
    assert prior_row_after.used_at is None

    # New row: revoked_at IS NULL.
    new_raw_token = captured_invite_link["invite_link"].rsplit("token=", 1)[1]
    new_token_row = await InviteTokenRepository(db_session).consume(new_raw_token)
    assert new_token_row is not None
    assert new_token_row.id != prior_row_id
    assert new_token_row.revoked_at is None


async def test_accepting_an_invite_with_a_revoked_token_fails_generically(
    db_session: AsyncSession,
) -> None:
    """A revoked token's raw value must be rejected by `accept_invite` with
    the same generic failure used for an expired/already-used token — no
    differentiating message."""
    org, _admin = await _make_org_and_admin(db_session)
    user = await _make_pending_user(db_session, org)

    repo = InviteTokenRepository(db_session)
    raw_token = await repo.issue(user.id)
    await repo.revoke_unused_for_user(user.id)
    await db_session.flush()

    with pytest.raises(auth_service.InviteAcceptError) as exc_info:
        await auth_service.accept_invite(
            db_session, raw_token=raw_token, new_password="a-new-password-123"
        )

    assert str(exc_info.value) == "invalid or expired invite token"

    # The user must remain untouched (still pending, no password set) —
    # confirms the failure short-circuited before any mutation.
    result = await db_session.execute(select(User).where(User.id == user.id))
    unchanged_user = result.scalar_one()
    assert unchanged_user.status == UserStatus.PENDING
    assert unchanged_user.password_hash is None
