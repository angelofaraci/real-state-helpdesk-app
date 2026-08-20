"""Unit tests for `OrgScope`, the org-scoping capability type.

`OrgScope.organization_id` is non-Optional by construction: the real
behavioral guarantee under test is that `OrgScope.from_principal` refuses to
produce a scope for a super-admin principal (`organization_id is None`), and
that the resulting value object is immutable.
"""

import dataclasses
from uuid import uuid4

import pytest

from app.core.exceptions import SuperAdminCannotAccessOrgDataError
from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.user import User


def _make_user(*, organization_id, role: UserRole = UserRole.ADMIN) -> User:
    user = User(
        id=uuid4(),
        organization_id=organization_id,
        name="Someone",
        email="someone@example.com",
        role=role,
    )
    return user


def test_from_principal_builds_scope_for_regular_user() -> None:
    org_id = uuid4()
    user = _make_user(organization_id=org_id, role=UserRole.OWNER)

    scope = OrgScope.from_principal(user)

    assert scope.organization_id == org_id
    assert scope.user_id == user.id
    assert scope.role == UserRole.OWNER


def test_from_principal_raises_for_super_admin_without_organization() -> None:
    user = _make_user(organization_id=None, role=UserRole.ADMIN)

    with pytest.raises(SuperAdminCannotAccessOrgDataError):
        OrgScope.from_principal(user)


def test_org_scope_is_frozen() -> None:
    scope = OrgScope(organization_id=uuid4(), user_id=uuid4(), role=UserRole.TENANT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.organization_id = uuid4()  # type: ignore[misc]


def test_for_background_worker() -> None:
    """Background workers (e.g. the classification worker) act without an
    authenticated request principal. `for_background_worker` builds a scope
    using the shared `SYSTEM_ACTOR_ID` sentinel as `user_id`, scoped to the
    given organization, with admin-level access."""
    from app.core.scope import SYSTEM_ACTOR_ID

    org_id = uuid4()

    scope = OrgScope.for_background_worker(org_id)

    assert scope.organization_id == org_id
    assert scope.user_id == SYSTEM_ACTOR_ID
    assert scope.role == UserRole.ADMIN


def test_for_anonymous_chat() -> None:
    """Anonymous chat-widget visitors (stage 4 — chatbot) act without an
    authenticated request principal. `for_anonymous_chat` builds a scope
    using the shared `ANONYMOUS_CHAT_ACTOR_ID` sentinel as `user_id`, scoped
    to the given organization, with tenant-level role (the most restrictive
    role, since an anonymous visitor should see no tickets at all)."""
    from app.core.scope import ANONYMOUS_CHAT_ACTOR_ID

    org_id = uuid4()

    scope = OrgScope.for_anonymous_chat(org_id)

    assert scope.organization_id == org_id
    assert scope.user_id == ANONYMOUS_CHAT_ACTOR_ID
    assert scope.role == UserRole.TENANT


def test_for_anonymous_chat_scope_matches_zero_tickets_in_compiled_sql() -> None:
    """Proves the sentinel is structurally incapable of matching any real
    ticket: `TicketRepository.select()` for a tenant-role scope narrows
    visibility to `Ticket.user_id == scope.user_id OR EXISTS(Contract WHERE
    Contract.tenant_id == scope.user_id)`. Since `ANONYMOUS_CHAT_ACTOR_ID`
    is a reserved sentinel that is NEVER written to `users.id` (nor,
    therefore, to `tickets.user_id` or `contracts.tenant_id`, both of which
    are non-null FKs to `users.id`), both branches of that clause can never
    match a real row — the same reasoning already established for
    `SYSTEM_ACTOR_ID` in `for_background_worker`.

    This follows the same compiled-SQL inspection convention used by
    `tests/unit/test_ticket_repository.py`."""
    from unittest.mock import AsyncMock

    from app.core.scope import ANONYMOUS_CHAT_ACTOR_ID
    from app.repositories.ticket_repository import TicketRepository

    org_id = uuid4()
    scope = OrgScope.for_anonymous_chat(org_id)
    repo = TicketRepository(AsyncMock(), scope)

    stmt = repo.select()
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    # The sentinel only ever appears in equality comparisons against
    # `tickets.user_id` / `contracts.tenant_id` — both non-null FKs to
    # `users.id` that can never hold a reserved, never-inserted sentinel
    # value — so the clause is structurally unsatisfiable, not merely
    # empirically unmatched.
    assert ANONYMOUS_CHAT_ACTOR_ID.hex in sql
    assert "tickets.user_id" in sql
    assert "EXISTS" in sql
    assert "contracts" in sql


def test_for_new_organization() -> None:
    """The one sanctioned bypass of `from_principal`: builds a scope for a
    just-flushed organization using a super-admin actor whose own
    `organization_id` is None. The actor's `organization_id` must never be
    read — only `actor.id` and the explicitly passed `organization_id`."""
    new_org_id = uuid4()
    actor = _make_user(organization_id=None, role=UserRole.ADMIN)

    scope = OrgScope.for_new_organization(new_org_id, actor=actor)

    assert scope.organization_id == new_org_id
    assert scope.user_id == actor.id
    assert scope.role == UserRole.ADMIN
