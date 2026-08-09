"""Unit tests for `app.services.user_service` (admin-driven invite flow).

No live Postgres is available in this sandbox. These tests mock the
`AsyncSession` boundary and the `InviteTokenRepository`/`mailer_service`
collaborators, so they exercise only the service's decision logic.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.services import user_service
from app.services.user_service import ConflictError, ForbiddenError, NotFoundError


def _make_admin(**overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        name="Admin User",
        email="admin@example.com",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    admin = MagicMock(spec=User)
    for key, value in defaults.items():
        setattr(admin, key, value)
    return admin


@pytest.fixture(autouse=True)
def _mock_collaborators(monkeypatch):
    repo_instance = AsyncMock()
    repo_instance.issue.return_value = "raw-invite-token"
    monkeypatch.setattr(
        user_service, "InviteTokenRepository", MagicMock(return_value=repo_instance)
    )
    mock_mailer = AsyncMock()
    monkeypatch.setattr(user_service, "send_invite_email", mock_mailer)
    return repo_instance, mock_mailer


@pytest.mark.asyncio
async def test_invite_user_creates_a_pending_user_and_sends_the_invite_email(
    _mock_collaborators,
) -> None:
    repo_instance, mock_mailer = _mock_collaborators
    admin = _make_admin()
    session = AsyncMock()
    session.add = MagicMock()

    user = await user_service.invite_user(
        session, admin=admin, name="Jane Doe", email="jane@example.com", role=UserRole.AGENT
    )

    session.add.assert_called_once_with(user)
    assert user.organization_id == admin.organization_id
    assert user.status == UserStatus.PENDING
    assert user.password_hash is None
    assert user.role == UserRole.AGENT
    repo_instance.issue.assert_awaited_once()
    mock_mailer.assert_awaited_once()
    assert mock_mailer.call_args.kwargs["to"] == "jane@example.com"
    assert "raw-invite-token" in mock_mailer.call_args.kwargs["invite_link"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_overrides", [{"role": UserRole.AGENT}, {"organization_id": None}]
)
async def test_invite_user_rejects_a_non_org_admin(admin_overrides: dict) -> None:
    admin = _make_admin(**admin_overrides)
    session = AsyncMock()

    with pytest.raises(ForbiddenError):
        await user_service.invite_user(
            session, admin=admin, name="Jane Doe", email="jane@example.com", role=UserRole.AGENT
        )


def _session_returning_user(user) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = user
    session.execute.return_value = execute_result
    return session


def _make_target_user(**overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        name="Pending User",
        email="pending@example.com",
        role=UserRole.AGENT,
        status=UserStatus.PENDING,
    )
    defaults.update(overrides)
    user = MagicMock(spec=User)
    for key, value in defaults.items():
        setattr(user, key, value)
    return user


@pytest.mark.asyncio
async def test_reissue_invite_deletes_prior_token_and_sends_a_new_one(
    _mock_collaborators,
) -> None:
    repo_instance, mock_mailer = _mock_collaborators
    org_id = uuid4()
    admin = _make_admin(organization_id=org_id)
    target = _make_target_user(organization_id=org_id)
    session = _session_returning_user(target)

    result = await user_service.reissue_invite(session, admin=admin, user_id=target.id)

    assert result is target
    repo_instance.delete_unused_for_user.assert_awaited_once_with(target.id)
    repo_instance.issue.assert_awaited_once()
    mock_mailer.assert_awaited_once()


@pytest.mark.asyncio
async def test_reissue_invite_rejects_a_user_from_a_different_org() -> None:
    admin = _make_admin(organization_id=uuid4())
    target = _make_target_user(organization_id=uuid4())
    session = _session_returning_user(target)

    with pytest.raises(NotFoundError):
        await user_service.reissue_invite(session, admin=admin, user_id=target.id)


@pytest.mark.asyncio
async def test_reissue_invite_rejects_an_already_active_user() -> None:
    org_id = uuid4()
    admin = _make_admin(organization_id=org_id)
    target = _make_target_user(organization_id=org_id, status=UserStatus.ACTIVE)
    session = _session_returning_user(target)

    with pytest.raises(ConflictError):
        await user_service.reissue_invite(session, admin=admin, user_id=target.id)


@pytest.mark.asyncio
async def test_reissue_invite_rejects_unknown_user() -> None:
    admin = _make_admin()
    session = _session_returning_user(None)

    with pytest.raises(NotFoundError):
        await user_service.reissue_invite(session, admin=admin, user_id=uuid4())
