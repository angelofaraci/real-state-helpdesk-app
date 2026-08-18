"""`OrgScope` — the capability required to construct any `ScopedRepository`.

Its whole purpose is to make it structurally impossible to run a
tenant-scoped query without a concrete, non-null `organization_id`. There is
deliberately no way to construct an `OrgScope` for a super-admin principal
(`User.organization_id is None`): `from_principal` raises instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from app.core.exceptions import SuperAdminCannotAccessOrgDataError
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.user import User


# Sentinel `user_id` for scopes constructed on behalf of a background
# worker (e.g. the async classification worker), which has no authenticated
# request principal. This is NOT a real user: it must NEVER be written to
# any FK column that references `users.id` (e.g. `tickets.agent_id`,
# `messages` authorship). It exists solely to satisfy `OrgScope.user_id`'s
# non-Optional shape for audit/logging purposes in worker contexts.
SYSTEM_ACTOR_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000001")

# Sentinel `user_id` for scopes constructed on behalf of an anonymous
# chat-widget visitor (stage 4 — chatbot), who has no authenticated request
# principal and no `users` row at all. Like `SYSTEM_ACTOR_ID`, this must
# NEVER be written to any FK column that references `users.id`. Its purpose
# is the opposite of `for_background_worker`'s admin-level access: it must
# match ZERO rows under `TicketRepository`'s tenant-role visibility clause
# (`Ticket.user_id == scope.user_id OR EXISTS(... Contract.tenant_id ==
# scope.user_id ...)`), since no real user or contract can ever carry this
# id — an anonymous chat session has no ticket/contract visibility at all.
ANONYMOUS_CHAT_ACTOR_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000002")


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

    @classmethod
    def for_background_worker(cls, organization_id: UUID) -> OrgScope:
        """Build a scope for background/worker contexts (e.g. the async
        classification worker), which have no authenticated request
        principal. Uses the `SYSTEM_ACTOR_ID` sentinel as `user_id` — never
        a real user — with admin-level role for unrestricted org access.
        """
        return cls(
            organization_id=organization_id,
            user_id=SYSTEM_ACTOR_ID,
            role=UserRole.ADMIN,
        )

    @classmethod
    def for_anonymous_chat(cls, organization_id: UUID) -> OrgScope:
        """Build a scope for an anonymous chat-widget visitor (stage 4 —
        chatbot), who has no authenticated request principal. Uses the
        `ANONYMOUS_CHAT_ACTOR_ID` sentinel as `user_id` — never a real
        user — with tenant-level role, so any ticket-visibility query run
        under this scope structurally matches zero rows (no real ticket or
        contract can ever carry this sentinel id).
        """
        return cls(
            organization_id=organization_id,
            user_id=ANONYMOUS_CHAT_ACTOR_ID,
            role=UserRole.TENANT,
        )
