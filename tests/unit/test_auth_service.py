"""Unit tests for `app.services.auth_service`.

No live Postgres is available in this sandbox. These tests mock the
`AsyncSession` boundary and the `RefreshTokenRepository` collaborator, so
they exercise only the service's decision logic (which is pure Python
control flow over ORM objects) — not real SQL execution against
Postgres-native column types. Anything that needs a live database is
explicitly out of scope here; see the xfail-marked integration test.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.security import hash_password
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.services import auth_service
from app.services.auth_service import AuthenticationError, InviteAcceptError


def _make_user(**overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        name="Jane Doe",
        email="jane@example.com",
        role=UserRole.AGENT,
        password_hash=hash_password("correct-horse-battery-staple"),
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    user = MagicMock(spec=User)
    for key, value in defaults.items():
        setattr(user, key, value)
    return user


def _session_returning_user(user) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = user
    session.execute.return_value = execute_result
    return session


@pytest.fixture(autouse=True)
def _mock_refresh_repo(monkeypatch):
    """Isolate service logic from `RefreshTokenRepository` by default;
    individual tests override `.issue`/`.rotate`/`.revoke*` behavior."""
    repo_instance = AsyncMock()
    repo_instance.issue.return_value = ("raw-refresh-token", uuid4())
    monkeypatch.setattr(
        auth_service, "RefreshTokenRepository", MagicMock(return_value=repo_instance)
    )
    return repo_instance


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_credentials() -> None:
    user = _make_user()
    session = _session_returning_user(user)

    pair = await auth_service.login(
        session, email="jane@example.com", password="correct-horse-battery-staple"
    )

    assert pair.access_token
    assert pair.refresh_token == "raw-refresh-token"


@pytest.mark.asyncio
async def test_login_rejects_unknown_email() -> None:
    session = _session_returning_user(None)

    with pytest.raises(AuthenticationError):
        await auth_service.login(session, email="ghost@example.com", password="whatever12345")


@pytest.mark.asyncio
async def test_login_rejects_wrong_password() -> None:
    user = _make_user()
    session = _session_returning_user(user)

    with pytest.raises(AuthenticationError):
        await auth_service.login(session, email="jane@example.com", password="totally-wrong")


@pytest.mark.asyncio
async def test_login_rejects_inactive_user() -> None:
    user = _make_user(status=UserStatus.DISABLED)
    session = _session_returning_user(user)

    with pytest.raises(AuthenticationError):
        await auth_service.login(
            session, email="jane@example.com", password="correct-horse-battery-staple"
        )


@pytest.mark.asyncio
async def test_login_rejects_pending_user_with_no_password_hash() -> None:
    user = _make_user(status=UserStatus.PENDING, password_hash=None)
    session = _session_returning_user(user)

    with pytest.raises(AuthenticationError):
        await auth_service.login(session, email="jane@example.com", password="whatever12345")


@pytest.mark.asyncio
async def test_login_failure_messages_are_identical_for_unknown_email_and_wrong_password() -> None:
    session_unknown = _session_returning_user(None)
    user = _make_user()
    session_wrong_password = _session_returning_user(user)

    with pytest.raises(AuthenticationError) as exc_unknown:
        await auth_service.login(session_unknown, email="ghost@example.com", password="x" * 12)
    with pytest.raises(AuthenticationError) as exc_wrong:
        await auth_service.login(
            session_wrong_password, email="jane@example.com", password="wrong-password"
        )

    assert str(exc_unknown.value) == str(exc_wrong.value)


@pytest.mark.asyncio
async def test_refresh_rotates_token_and_issues_new_pair(_mock_refresh_repo) -> None:
    user = _make_user()
    family_id = uuid4()
    token_row = MagicMock()
    token_row.revoked_at = None
    token_row.expires_at = datetime.now(UTC) + timedelta(days=1)
    token_row.user_id = user.id
    token_row.family_id = family_id
    _mock_refresh_repo.rotate.return_value = token_row

    session = _session_returning_user(user)

    pair = await auth_service.refresh(session, raw_refresh_token="some-raw-token")

    assert pair.access_token
    _mock_refresh_repo.revoke.assert_awaited_once_with(token_row)
    _mock_refresh_repo.issue.assert_awaited_once()
    assert _mock_refresh_repo.issue.call_args.kwargs["family_id"] == family_id


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_token(_mock_refresh_repo) -> None:
    _mock_refresh_repo.rotate.return_value = None
    session = AsyncMock()

    with pytest.raises(AuthenticationError):
        await auth_service.refresh(session, raw_refresh_token="does-not-exist")


@pytest.mark.asyncio
async def test_refresh_detects_reuse_and_revokes_entire_family(_mock_refresh_repo) -> None:
    family_id = uuid4()
    token_row = MagicMock()
    token_row.revoked_at = datetime.now(UTC)  # already rotated once before
    token_row.family_id = family_id
    _mock_refresh_repo.rotate.return_value = token_row
    session = AsyncMock()

    with pytest.raises(AuthenticationError):
        await auth_service.refresh(session, raw_refresh_token="stolen-token")

    _mock_refresh_repo.revoke_family.assert_awaited_once_with(family_id)


@pytest.mark.asyncio
async def test_refresh_rejects_expired_token(_mock_refresh_repo) -> None:
    token_row = MagicMock()
    token_row.revoked_at = None
    token_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    _mock_refresh_repo.rotate.return_value = token_row
    session = AsyncMock()

    with pytest.raises(AuthenticationError):
        await auth_service.refresh(session, raw_refresh_token="expired-token")


@pytest.mark.asyncio
async def test_refresh_rejects_when_user_no_longer_active(_mock_refresh_repo) -> None:
    user = _make_user(status=UserStatus.DISABLED)
    token_row = MagicMock()
    token_row.revoked_at = None
    token_row.expires_at = datetime.now(UTC) + timedelta(days=1)
    token_row.user_id = user.id
    _mock_refresh_repo.rotate.return_value = token_row

    session = _session_returning_user(user)

    with pytest.raises(AuthenticationError):
        await auth_service.refresh(session, raw_refresh_token="some-raw-token")


@pytest.mark.asyncio
async def test_logout_revokes_the_presented_tokens_family(_mock_refresh_repo) -> None:
    family_id = uuid4()
    token_row = MagicMock()
    token_row.family_id = family_id
    _mock_refresh_repo.rotate.return_value = token_row
    session = AsyncMock()

    await auth_service.logout(session, raw_refresh_token="some-raw-token")

    _mock_refresh_repo.revoke_family.assert_awaited_once_with(family_id)


@pytest.mark.asyncio
async def test_logout_is_a_no_op_for_an_unknown_token(_mock_refresh_repo) -> None:
    _mock_refresh_repo.rotate.return_value = None
    session = AsyncMock()

    await auth_service.logout(session, raw_refresh_token="does-not-exist")

    _mock_refresh_repo.revoke_family.assert_not_awaited()


@pytest.fixture
def _mock_invite_repo(monkeypatch):
    repo_instance = AsyncMock()
    monkeypatch.setattr(
        auth_service, "InviteTokenRepository", MagicMock(return_value=repo_instance)
    )
    return repo_instance


def _make_invite_token_row(**overrides):
    defaults = dict(
        user_id=uuid4(),
        used_at=None,
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    defaults.update(overrides)
    row = MagicMock()
    for key, value in defaults.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
async def test_accept_invite_activates_pending_user_and_issues_a_token_pair(
    _mock_invite_repo, _mock_refresh_repo
) -> None:
    pending_user = _make_user(status=UserStatus.PENDING, password_hash=None)
    token_row = _make_invite_token_row(user_id=pending_user.id)
    _mock_invite_repo.consume.return_value = token_row
    session = _session_returning_user(pending_user)

    pair = await auth_service.accept_invite(
        session, raw_token="raw-invite-token", new_password="a-strong-password"
    )

    assert pair.access_token
    assert pending_user.status == UserStatus.ACTIVE
    assert pending_user.password_hash is not None
    _mock_invite_repo.mark_used.assert_awaited_once_with(token_row)


@pytest.mark.asyncio
async def test_accept_invite_rejects_an_unknown_token(_mock_invite_repo) -> None:
    _mock_invite_repo.consume.return_value = None
    session = AsyncMock()

    with pytest.raises(InviteAcceptError):
        await auth_service.accept_invite(
            session, raw_token="does-not-exist", new_password="a-strong-password"
        )


@pytest.mark.asyncio
async def test_accept_invite_rejects_an_already_used_token(_mock_invite_repo) -> None:
    token_row = _make_invite_token_row(used_at=datetime.now(UTC))
    _mock_invite_repo.consume.return_value = token_row
    session = AsyncMock()

    with pytest.raises(InviteAcceptError):
        await auth_service.accept_invite(
            session, raw_token="reused-token", new_password="a-strong-password"
        )


@pytest.mark.asyncio
async def test_accept_invite_rejects_a_revoked_token(_mock_invite_repo) -> None:
    token_row = _make_invite_token_row(revoked_at=datetime.now(UTC))
    _mock_invite_repo.consume.return_value = token_row
    session = AsyncMock()

    with pytest.raises(InviteAcceptError) as exc_info:
        await auth_service.accept_invite(
            session, raw_token="revoked-token", new_password="a-strong-password"
        )

    # Same generic message as every other invite-acceptance failure — no
    # new user-facing differentiation for revoked tokens.
    assert str(exc_info.value) == auth_service._GENERIC_INVITE_FAILURE


@pytest.mark.asyncio
async def test_accept_invite_rejects_an_expired_token(_mock_invite_repo) -> None:
    token_row = _make_invite_token_row(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    _mock_invite_repo.consume.return_value = token_row
    session = AsyncMock()

    with pytest.raises(InviteAcceptError):
        await auth_service.accept_invite(
            session, raw_token="expired-token", new_password="a-strong-password"
        )


@pytest.mark.asyncio
async def test_accept_invite_rejects_a_token_for_a_non_pending_user(
    _mock_invite_repo,
) -> None:
    active_user = _make_user(status=UserStatus.ACTIVE)
    token_row = _make_invite_token_row(user_id=active_user.id)
    _mock_invite_repo.consume.return_value = token_row
    session = _session_returning_user(active_user)

    with pytest.raises(InviteAcceptError):
        await auth_service.accept_invite(
            session, raw_token="orphaned-token", new_password="a-strong-password"
        )


@pytest.mark.asyncio
async def test_accept_invite_rejects_an_orphaned_token_with_no_user(
    _mock_invite_repo,
) -> None:
    token_row = _make_invite_token_row()
    _mock_invite_repo.consume.return_value = token_row
    session = _session_returning_user(None)

    with pytest.raises(InviteAcceptError):
        await auth_service.accept_invite(
            session, raw_token="orphaned-token", new_password="a-strong-password"
        )
