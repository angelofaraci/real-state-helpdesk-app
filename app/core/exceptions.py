"""Domain exception hierarchy for the core layer.

`NotFoundError` here is the SINGLE site in the codebase that signals "row
does not exist, or exists but is not visible to the caller's `OrgScope`" —
nothing else should raise it directly except `ScopedRepository.get_or_404`.
(This is deliberately distinct from `app.services.user_service.NotFoundError`,
a pre-existing service-level error for the not-yet-migrated invite flow.)
"""

from uuid import UUID


class DomainError(Exception):
    """Base class for domain-level errors raised by the app's core layer."""


class NotFoundError(DomainError):
    """A row does not exist, or is not visible to the caller's `OrgScope`.

    These two cases are intentionally indistinguishable from the caller's
    point of view: a scoped lookup must never leak whether a row exists in
    another organization.
    """

    def __init__(self, name: str, id: UUID | str) -> None:  # noqa: A002
        self.name = name
        self.id = id
        super().__init__(f"{name} not found: {id}")


class SuperAdminCannotAccessOrgDataError(DomainError):
    """Raised when building an `OrgScope` for a super-admin principal.

    A super-admin `User` has `organization_id is None` by design (see the
    `ck_users_organization_required_unless_admin` check constraint) and
    therefore has no tenant data scope to operate within.
    """

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        super().__init__(
            f"user {user_id} is a super-admin and has no organization scope"
        )
