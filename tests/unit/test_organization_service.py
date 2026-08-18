"""Unit tests for `app.services.organization_service` (super-admin CRUD).

Authorization (super-admin only) is enforced at the API layer via
`require_super_admin`, not here — these tests exercise only the service's
persistence/conflict-mapping logic against a mocked `AsyncSession`.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.user import User
from app.services import organization_service
from app.services.organization_service import ConflictError


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


def _super_admin() -> User:
    return User(
        id=uuid4(),
        organization_id=None,
        name="Root",
        email="root@example.com",
        role=UserRole.ADMIN,
    )


@pytest.mark.asyncio
async def test_create_organization_adds_and_flushes() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    actor = _super_admin()

    with patch(
        "app.services.organization_service.taxonomy_seed.seed_default_taxonomy",
        AsyncMock(),
    ):
        org = await organization_service.create_organization(
            session, name="Acme Corp", actor=actor
        )

    assert org.name == "Acme Corp"
    session.add.assert_called_once_with(org)


@pytest.mark.asyncio
async def test_create_organization_raises_conflict_on_duplicate_name() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await organization_service.create_organization(
            session, name="Acme Corp", actor=_super_admin()
        )


@pytest.mark.asyncio
async def test_create_organization_does_not_seed_taxonomy_on_name_conflict() -> None:
    """The org-first flush must raise 409 BEFORE any taxonomy row is
    staged — no scope can be built for an org that failed to persist."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with (
        patch(
            "app.services.organization_service.taxonomy_seed.seed_default_taxonomy",
            AsyncMock(),
        ) as seed_call,
        pytest.raises(ConflictError),
    ):
        await organization_service.create_organization(
            session, name="Acme Corp", actor=_super_admin()
        )

    seed_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_organization_seeds_default_taxonomy_via_a_scope_for_the_new_org() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    actor = _super_admin()

    with patch(
        "app.services.organization_service.taxonomy_seed.seed_default_taxonomy",
        AsyncMock(),
    ) as seed_call:
        org = await organization_service.create_organization(
            session, name="Acme Corp", actor=actor
        )

    seed_call.assert_awaited_once()
    _, kwargs = seed_call.await_args
    scope: OrgScope = kwargs["scope"]
    assert scope.organization_id == org.id
    assert scope.user_id == actor.id
    assert scope.role == UserRole.ADMIN


@pytest.mark.asyncio
async def test_create_organization_flushes_once_for_the_org_and_once_after_seeding() -> None:
    session = AsyncMock()
    session.add = MagicMock()

    with patch(
        "app.services.organization_service.taxonomy_seed.seed_default_taxonomy",
        AsyncMock(),
    ):
        await organization_service.create_organization(
            session, name="Acme Corp", actor=_super_admin()
        )

    assert session.flush.await_count == 2


@pytest.mark.asyncio
async def test_create_organization_generates_a_unique_chat_widget_key() -> None:
    """Stage 4 — chatbot: every newly created org must get a unique
    `chat_widget_key` at creation time, not only via the migration 0005
    backfill (which only covers orgs that existed before that migration)."""
    session = AsyncMock()
    session.add = MagicMock()

    with patch(
        "app.services.organization_service.taxonomy_seed.seed_default_taxonomy",
        AsyncMock(),
    ):
        org_a = await organization_service.create_organization(
            session, name="Acme Corp", actor=_super_admin()
        )
        org_b = await organization_service.create_organization(
            session, name="Beta Corp", actor=_super_admin()
        )

    assert org_a.chat_widget_key
    assert org_b.chat_widget_key
    assert org_a.chat_widget_key != org_b.chat_widget_key


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
