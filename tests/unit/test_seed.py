"""Unit tests for `app.core.seed.seed_super_admin`.

`AsyncSession` is mocked, following the pattern established in
`tests/unit/test_user_service_crud.py`. There is no live Postgres in this
sandbox, so the DB-level CHECK constraint (`organization_id IS NOT NULL OR
role = 'admin'`) and the `python -m app.core.seed` entrypoint end-to-end are
NOT covered here — see the apply-progress report for what remains
unverified against a real database.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.security import verify_password
from app.core.seed import seed_super_admin
from app.models.enums import UserRole, UserStatus
from app.models.user import User


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_seed_super_admin_creates_one_when_none_exists() -> None:
    session = _session_returning_scalar(None)

    admin = await seed_super_admin(
        session, email="root@helpdesk.test", password="s3cret-pass", name="Root Admin"
    )

    assert admin.organization_id is None
    assert admin.role == UserRole.ADMIN
    assert admin.status == UserStatus.ACTIVE
    assert admin.email == "root@helpdesk.test"
    assert admin.name == "Root Admin"
    assert admin.password_hash is not None
    assert verify_password(admin.password_hash, "s3cret-pass")

    session.add.assert_called_once_with(admin)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_super_admin_returns_existing_one_without_recreating() -> None:
    existing = User(
        id=uuid4(),
        organization_id=None,
        name="Existing Root",
        email="root@helpdesk.test",
        role=UserRole.ADMIN,
        password_hash="already-hashed",
        status=UserStatus.ACTIVE,
    )
    session = _session_returning_scalar(existing)

    admin = await seed_super_admin(
        session, email="ignored@helpdesk.test", password="ignored-password"
    )

    assert admin is existing
    assert admin.password_hash == "already-hashed"
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
