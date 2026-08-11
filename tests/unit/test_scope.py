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
