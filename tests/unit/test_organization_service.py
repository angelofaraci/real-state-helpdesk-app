"""Unit tests for `app.services.organization_service` (super-admin CRUD).

Authorization (super-admin only) is enforced at the API layer via
`require_super_admin`, not here — these tests exercise only the service's
persistence/conflict-mapping logic against a mocked `AsyncSession`.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.organization import Organization
from app.services import organization_service
from app.services.organization_service import ConflictError


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_create_organization_adds_and_flushes() -> None:
    session = AsyncMock()
    session.add = MagicMock()

    org = await organization_service.create_organization(session, name="Acme Corp")

    assert org.name == "Acme Corp"
    session.add.assert_called_once_with(org)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_organization_raises_conflict_on_duplicate_name() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await organization_service.create_organization(session, name="Acme Corp")


@pytest.mark.asyncio
async def test_get_organization_returns_the_row() -> None:
    fake_org = MagicMock(spec=Organization)
    session = _session_returning(fake_org)

    result = await organization_service.get_organization(session, organization_id=uuid4())

    assert result is fake_org


@pytest.mark.asyncio
async def test_update_organization_applies_fields_and_flushes() -> None:
    existing = Organization(id=uuid4(), name="Old Name")
    session = _session_returning(existing)

    updated = await organization_service.update_organization(
        session, organization_id=existing.id, name="New Name"
    )

    assert updated.name == "New Name"
    session.flush.assert_awaited_once()
