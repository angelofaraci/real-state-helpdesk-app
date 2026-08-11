"""Unit tests for `app.services.taxonomy_seed.seed_default_taxonomy`.

`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_category_service.py`: `ScopedRepository.add` calls
`session.add` synchronously, so `session.add` is replaced with a
`MagicMock` to record every staged instance without producing an
unawaited-coroutine warning.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.scope import OrgScope
from app.core.taxonomy_defaults import DEFAULT_CATEGORIES, DEFAULT_URGENCY_LEVELS
from app.models.category import Category
from app.models.enums import UserRole
from app.models.urgency_level import UrgencyLevel
from app.services.taxonomy_seed import seed_default_taxonomy


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid4(), user_id=uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


class _RecordingSession:
    """Minimal stand-in for `AsyncSession` — only `.add` is used by
    `ScopedRepository.add`, so nothing else needs to exist here."""

    def __init__(self) -> None:
        self.add = MagicMock()


@pytest.mark.asyncio
async def test_seed_default_taxonomy_creates_scoped_rows() -> None:
    scope = _scope()
    session = _RecordingSession()

    await seed_default_taxonomy(session, scope=scope)

    added = [call.args[0] for call in session.add.call_args_list]
    assert len(added) == 10

    categories = [row for row in added if isinstance(row, Category)]
    urgency_levels = [row for row in added if isinstance(row, UrgencyLevel)]
    assert len(categories) == 6
    assert len(urgency_levels) == 4
    assert all(row.organization_id == scope.organization_id for row in added)
    assert {c.name for c in categories} == {seed.name for seed in DEFAULT_CATEGORIES}
    assert {u.name for u in urgency_levels} == {
        seed.name for seed in DEFAULT_URGENCY_LEVELS
    }


@pytest.mark.asyncio
async def test_seed_default_taxonomy_scopes_rows_to_the_passed_organization() -> None:
    """Triangulation: a second, DIFFERENT scope produces rows tagged with
    that scope's own organization_id — proving the org id is not
    hardcoded/leaked from a prior call."""
    other_scope = _scope(organization_id=uuid4())
    session = _RecordingSession()

    await seed_default_taxonomy(session, scope=other_scope)

    added = [call.args[0] for call in session.add.call_args_list]
    assert all(row.organization_id == other_scope.organization_id for row in added)
