"""Unit tests for `app.services.urgency_level_service`. Mirrors
`tests/unit/test_category_service.py` — same shape, `sla_hours`/`sort_order`
in place of `description`.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.models.urgency_level import UrgencyLevel
from app.services import urgency_level_service
from app.services.taxonomy_service import TaxonomyDeleteResult
from app.services.urgency_level_service import ConflictError


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _urgency(scope: OrgScope, **overrides) -> UrgencyLevel:
    defaults = dict(
        id=uuid4(),
        organization_id=scope.organization_id,
        name="Critical",
        sla_hours=4,
        sort_order=0,
        active=True,
    )
    defaults.update(overrides)
    return UrgencyLevel(**defaults)


def _session_returning_scalar(scalar_result) -> AsyncMock:
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session = AsyncMock()
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_create_urgency_level_adds_and_flushes() -> None:
    scope = _scope()
    session = AsyncMock()
    session.add = MagicMock()

    urgency = await urgency_level_service.create_urgency_level(
        session, scope=scope, name="Critical", sla_hours=4, sort_order=1
    )

    assert urgency.name == "Critical"
    assert urgency.sla_hours == 4
    assert urgency.sort_order == 1
    assert urgency.organization_id == scope.organization_id
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_urgency_level_passes_through_respects_business_hours() -> None:
    """Stage 6 (PR4): the schema field is worthless if the service layer
    silently drops it — must reach the constructed `UrgencyLevel` row."""
    scope = _scope()
    session = AsyncMock()
    session.add = MagicMock()

    urgency = await urgency_level_service.create_urgency_level(
        session,
        scope=scope,
        name="Critical",
        sla_hours=4,
        sort_order=1,
        respects_business_hours=False,
    )

    assert urgency.respects_business_hours is False


@pytest.mark.asyncio
async def test_create_urgency_level_raises_conflict_on_duplicate_name() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await urgency_level_service.create_urgency_level(
            session, scope=_scope(), name="Critical", sla_hours=4, sort_order=0
        )


@pytest.mark.asyncio
async def test_list_urgency_levels_defaults_to_active_only() -> None:
    scope = _scope()
    rows = [_urgency(scope)]
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = execute_result

    with patch(
        "app.services.urgency_level_service.UrgencyLevelRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        result = await urgency_level_service.list_urgency_levels(session, scope=scope)

    repo.list.assert_called_once_with(active=True)
    assert result == rows


@pytest.mark.asyncio
async def test_get_urgency_level_returns_the_row_in_scope() -> None:
    scope = _scope()
    target = _urgency(scope)
    session = _session_returning_scalar(target)

    result = await urgency_level_service.get_urgency_level(
        session, scope=scope, urgency_level_id=target.id
    )

    assert result is target


@pytest.mark.asyncio
async def test_get_urgency_level_raises_not_found_when_out_of_scope() -> None:
    session = _session_returning_scalar(None)

    with pytest.raises(NotFoundError):
        await urgency_level_service.get_urgency_level(
            session, scope=_scope(), urgency_level_id=uuid4()
        )


@pytest.mark.asyncio
async def test_update_urgency_level_applies_fields_and_flushes() -> None:
    scope = _scope()
    target = _urgency(scope, sla_hours=4)
    session = _session_returning_scalar(target)

    updated = await urgency_level_service.update_urgency_level(
        session, scope=scope, urgency_level_id=target.id, sla_hours=8
    )

    assert updated.sla_hours == 8
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_urgency_level_delegates_to_the_shared_taxonomy_helper() -> None:
    scope = _scope()
    session = AsyncMock()
    expected = TaxonomyDeleteResult(id=uuid4(), deleted=False, active=False)

    with patch(
        "app.services.urgency_level_service.delete_or_deactivate",
        AsyncMock(return_value=expected),
    ) as helper:
        result = await urgency_level_service.delete_urgency_level(
            session, scope=scope, urgency_level_id=expected.id
        )

    helper.assert_awaited_once()
    assert result == expected
