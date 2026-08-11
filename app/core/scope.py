"""`OrgScope` — the capability required to construct any `ScopedRepository`.

Its whole purpose is to make it structurally impossible to run a
tenant-scoped query without a concrete, non-null `organization_id`. There is
deliberately no way to construct an `OrgScope` for a super-admin principal
(`User.organization_id is None`): `from_principal` raises instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.exceptions import SuperAdminCannotAccessOrgDataError
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.user import User


@dataclass(frozen=True, slots=True)
class OrgScope:
    """Immutable capability carrying the tenant scope for a single request.

    `organization_id` is non-Optional by construction: a static type
    checker rejects `OrgScope(organization_id=None, ...)` outright, and
    there is no code path that produces an `OrgScope` from a super-admin
    principal (see `from_principal`).
    """

    organization_id: UUID
    user_id: UUID
    role: UserRole

    @classmethod
    def from_principal(cls, user: User) -> OrgScope:
        """Build the scope for an authenticated `User`.

        Raises `SuperAdminCannotAccessOrgDataError` if `user.organization_id`
        is `None` (super-admins have no tenant data of their own).
        """
        if user.organization_id is None:
            raise SuperAdminCannotAccessOrgDataError(user.id)
        return cls(
            organization_id=user.organization_id,
            user_id=user.id,
            role=user.role,
        )

    @classmethod
    def for_new_organization(cls, organization_id: UUID, *, actor: User) -> OrgScope:
        """The ONLY sanctioned way to obtain a scope without an org-scoped
        principal. Reserved for seeding the default taxonomy of an
        organization just created and flushed by a super-admin (whose own
        `organization_id` is None, so `from_principal` cannot apply).
        `organization_id` MUST be that just-flushed org's id;
        `actor.organization_id` is never read.
        """
        return cls(organization_id=organization_id, user_id=actor.id, role=UserRole.ADMIN)
