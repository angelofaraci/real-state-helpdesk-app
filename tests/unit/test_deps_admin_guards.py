"""Unit tests for the admin role-guard dependencies in `app.api.deps`:
`require_super_admin` (organizations) and `require_org_admin` (users CRUD,
bridges the authenticated `User` into an `OrgScope`).
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.deps import require_org_admin, require_super_admin
from app.core.scope import OrgScope
from app.models.enums import UserRole, UserStatus
from app.models.user import User


def _user(**overrides) -> User:
    defaults = dict(
        id=uuid4(),
        organization_id=uuid4(),
        name="Someone",
        email="someone@example.com",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.mark.asyncio
async def test_require_super_admin_accepts_a_super_admin_principal() -> None:
    principal = _user(organization_id=None, role=UserRole.ADMIN)

    result = await require_super_admin(principal)

    assert result is principal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"organization_id": uuid4(), "role": UserRole.ADMIN},  # regular org admin
        {"organization_id": None, "role": UserRole.AGENT},  # non-admin role, no org
    ],
)
async def test_require_super_admin_rejects_non_super_admins(overrides: dict) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_super_admin(_user(**overrides))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_require_org_admin_returns_an_org_scope_for_an_org_admin() -> None:
    org_id = uuid4()
    principal = _user(organization_id=org_id, role=UserRole.ADMIN)

    scope = await require_org_admin(principal)

    assert isinstance(scope, OrgScope)
    assert scope.organization_id == org_id
    assert scope.user_id == principal.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"role": UserRole.AGENT},  # non-admin role
        {"organization_id": None, "role": UserRole.ADMIN},  # super-admin, no org
    ],
)
async def test_require_org_admin_rejects_non_org_admins(overrides: dict) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_org_admin(_user(**overrides))

    assert exc_info.value.status_code == 403
