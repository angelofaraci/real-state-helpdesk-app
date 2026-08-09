"""Admin-driven user invitation flow: invite a new user, reissue an invite.

Full user CRUD (list/get/patch/delete) is out of scope here; see a later
work unit. Org scoping here follows the same "plain repository, filter by
the principal's own `organization_id` directly" style established by
`RefreshTokenRepository` — the org-scoped `ScopedRepository` pattern is
introduced in a later work unit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.repositories.invite_token_repository import InviteTokenRepository
from app.services.mailer_service import send_invite_email


class UserServiceError(Exception):
    """Base class for invite-flow failures. Each subclass maps to a
    specific HTTP status at the API layer; the message is used verbatim as
    the response detail."""


class ForbiddenError(UserServiceError):
    """Raised when the acting principal lacks admin privileges to invite
    users into an organization."""


class NotFoundError(UserServiceError):
    """Raised when the target user does not exist or does not belong to
    the acting admin's organization."""


class ConflictError(UserServiceError):
    """Raised when the requested operation conflicts with the target
    user's current state (e.g. reissuing an invite for an active user)."""


async def invite_user(
    session: AsyncSession, *, admin: User, name: str, email: str, role: UserRole
) -> User:
    """Create a new `pending` user in the admin's organization and send
    them an invite email.

    Raises `ForbiddenError` unless `admin` is an org-scoped admin (a
    platform-level super-admin, `organization_id is None`, is not allowed
    to invite users into an organization at this stage).

    A duplicate email raises `sqlalchemy.exc.IntegrityError` on flush
    (from the `users.email` unique index), which the caller maps to 409.
    """
    _require_org_admin(admin)

    user = User(
        organization_id=admin.organization_id,
        name=name,
        email=email,
        role=role,
        password_hash=None,
        status=UserStatus.PENDING,
    )
    session.add(user)
    await session.flush()

    await _issue_and_send_invite(session, user)
    return user


async def reissue_invite(session: AsyncSession, *, admin: User, user_id: UUID) -> User:
    """Delete any outstanding invite for `user_id` and issue + send a
    fresh one.

    Raises `ForbiddenError` unless `admin` is an org-scoped admin,
    `NotFoundError` if the user does not exist or belongs to a different
    organization, and `ConflictError` if the user is no longer `pending`.
    """
    _require_org_admin(admin)

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or user.organization_id != admin.organization_id:
        raise NotFoundError("user not found")
    if user.status != UserStatus.PENDING:
        raise ConflictError("already_active")

    repo = InviteTokenRepository(session)
    await repo.delete_unused_for_user(user.id)
    await _issue_and_send_invite(session, user, repo=repo)
    return user


def _require_org_admin(admin: User) -> None:
    if admin.role != UserRole.ADMIN or admin.organization_id is None:
        raise ForbiddenError("admin privileges required")


async def _issue_and_send_invite(
    session: AsyncSession, user: User, *, repo: InviteTokenRepository | None = None
) -> None:
    repo = repo or InviteTokenRepository(session)
    raw_token = await repo.issue(user.id)
    settings = get_settings()
    invite_link = f"{settings.frontend_url}/accept-invite?token={raw_token}"
    await send_invite_email(to=user.email, invite_link=invite_link)
