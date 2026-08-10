"""Unit tests for `app.api.deps.require_org_member`: the dependency used by
ticket read routes, since ALL four org roles (tenant/owner/agent/admin) can
see tickets — just different subsets, enforced by `TicketRepository.select()`
itself, not by this dependency. This dependency only rejects a super-admin
(`organization_id is None`), which has no org scope at all.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.deps import require_org_member
from app.core.scope import OrgScope
from app.models.enums import UserRole, UserStatus
from app.models.user import User


def _user(**overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        name="Someone",
        email="someone@example.com",
        role=UserRole.TENANT,
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [UserRole.TENANT, UserRole.OWNER, UserRole.AGENT, UserRole.ADMIN]
)
async def test_require_org_member_returns_an_org_scope_for_any_org_role(role) -> None:
    org_id = uuid4()
    principal = _user(organization_id=org_id, role=role)

    scope = await require_org_member(principal)

    assert isinstance(scope, OrgScope)
    assert scope.organization_id == org_id
    assert scope.user_id == principal.id
    assert scope.role == role


@pytest.mark.asyncio
async def test_require_org_member_rejects_a_super_admin_with_403() -> None:
    principal = _user(organization_id=None, role=UserRole.ADMIN)

    with pytest.raises(HTTPException) as exc_info:
        await require_org_member(principal)

    assert exc_info.value.status_code == 403
