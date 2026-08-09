"""Unit tests for `ContractRepository`, the FIRST production repository to
exercise the join-path (`scope_path`) branch of `ScopedRepository` — this
proves Work Unit 3's join-scoping design actually produces the correct
`EXISTS`-through-`properties` SQL for a real model, not just the throwaway
fixtures in `tests/unit/test_scoped_repository.py`.

No live Postgres is available in this sandbox, so the join is verified by
inspecting the compiled SQL of the returned `Select`. Session-boundary
behavior (`get_or_404`) is verified against a mocked `AsyncSession`.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.contract_repository import ContractRepository


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


def test_scope_clause_uses_exists_through_properties() -> None:
    scope = _scope()
    repo = ContractRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "contracts" in sql
    assert "EXISTS" in sql
    assert "properties" in sql
    assert "properties.organization_id" in sql
    assert scope.organization_id.hex in sql
    # The join predicate must tie contracts.property_id to properties.id,
    # not filter contracts by a nonexistent organization_id column.
    assert "contracts.organization_id" not in sql
    assert "properties.id = contracts.property_id" in sql or "contracts.property_id = properties.id" in sql


@pytest.mark.asyncio
async def test_get_or_404_raises_not_found_when_property_out_of_scope() -> None:
    session = _session_returning(None)
    repo = ContractRepository(session, _scope())

    with pytest.raises(NotFoundError):
        await repo.get_or_404(uuid.uuid4())


@pytest.mark.asyncio
async def test_get_or_404_returns_the_row_when_in_scope() -> None:
    fake_row = MagicMock()
    session = _session_returning(fake_row)
    repo = ContractRepository(session, _scope())

    result = await repo.get_or_404(uuid.uuid4())

    assert result is fake_row
