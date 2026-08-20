"""Unit tests for `OrganizationRepository`, the plain (unscoped) repository
for `organizations`. Session-boundary behavior is verified against a mocked
`AsyncSession`, following the pattern established by
`tests/unit/test_scoped_repository.py`. `update()`'s not-found path is
covered indirectly via `get_or_404` (which it delegates to) and is not
duplicated here.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_get_or_404_returns_the_row_when_found() -> None:
    fake_org = MagicMock(spec=Organization)
    repo = OrganizationRepository(_session_returning(fake_org))

    assert await repo.get_or_404(uuid.uuid4()) is fake_org


@pytest.mark.asyncio
async def test_get_or_404_raises_not_found_error_when_missing() -> None:
    repo = OrganizationRepository(_session_returning(None))
    missing_id = uuid.uuid4()

    with pytest.raises(NotFoundError) as exc_info:
        await repo.get_or_404(missing_id)

    assert exc_info.value.id == missing_id
    assert exc_info.value.name == "Organization"


def test_add_stages_a_new_organization_on_the_session() -> None:
    session = MagicMock()
    repo = OrganizationRepository(session)

    org = repo.add(name="Acme Corp")

    assert org.name == "Acme Corp"
    session.add.assert_called_once_with(org)


@pytest.mark.asyncio
async def test_update_applies_fields_to_the_fetched_row() -> None:
    existing = Organization(id=uuid.uuid4(), name="Old Name")
    repo = OrganizationRepository(_session_returning(existing))

    updated = await repo.update(existing.id, name="New Name")

    assert updated.name == "New Name"


@pytest.mark.asyncio
async def test_list_returns_all_organizations() -> None:
    orgs = [MagicMock(spec=Organization), MagicMock(spec=Organization)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = orgs
    session = AsyncMock()
    session.execute.return_value = execute_result
    repo = OrganizationRepository(session)

    assert await repo.list() == orgs


@pytest.mark.asyncio
async def test_get_by_whatsapp_phone_number_id_returns_the_matching_org() -> None:
    fake_org = MagicMock(spec=Organization)
    session = _session_returning(fake_org)
    repo = OrganizationRepository(session)

    result = await repo.get_by_whatsapp_phone_number_id(session, "1234567890")

    assert result is fake_org


@pytest.mark.asyncio
async def test_get_by_whatsapp_phone_number_id_returns_none_when_no_match() -> None:
    session = _session_returning(None)
    repo = OrganizationRepository(session)

    result = await repo.get_by_whatsapp_phone_number_id(session, "0000000000")

    assert result is None


@pytest.mark.asyncio
async def test_get_by_support_email_address_returns_the_matching_org() -> None:
    fake_org = MagicMock(spec=Organization)
    session = _session_returning(fake_org)
    repo = OrganizationRepository(session)

    result = await repo.get_by_support_email_address(session, "Support@Acme.example")

    assert result is fake_org


@pytest.mark.asyncio
async def test_get_by_support_email_address_returns_none_when_no_match() -> None:
    session = _session_returning(None)
    repo = OrganizationRepository(session)

    result = await repo.get_by_support_email_address(session, "nobody@example.com")

    assert result is None
