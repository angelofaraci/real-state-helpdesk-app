"""Unit tests for `PropertyRepository`, the direct-scoped repository for
`Property` (owns its own `organization_id` column).

`get_or_404`/`add`/`update`/`delete` are already covered generically by
`tests/unit/test_scoped_repository.py`; these tests cover only what
`PropertyRepository` adds on top: excluding soft-deleted rows from
`select()` by default.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.property_repository import PropertyRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


def test_select_scopes_to_the_organization_and_excludes_soft_deleted() -> None:
    scope = _scope()
    repo = PropertyRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "properties" in sql
    assert scope.organization_id.hex in sql
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_get_or_404_raises_not_found_for_a_soft_deleted_property() -> None:
    # A soft-deleted property is invisible via the default select(), so the
    # repository-level query would never return it: simulate that here by
    # having the mocked session return no row.
    session = _session_returning(None)
    repo = PropertyRepository(session, _scope())

    with pytest.raises(NotFoundError):
        await repo.get_or_404(uuid.uuid4())
