"""Unit tests for `ScopedRepository`, the generic org-scoped repository base.

No live Postgres is available in this sandbox (same constraint noted by
prior work units). Query-building logic (`scope_clause` / `select`) is
verified by inspecting the compiled SQL of the returned `Select`, which
needs no DB connection. Session-boundary behavior (`get_or_404` / `add` /
`update` / `delete`) is verified against a mocked `AsyncSession`, following
the pattern established by `tests/unit/test_refresh_token_repository.py`.

Two throwaway, test-only mapped classes prove both scoping strategies:
- `DirectWidget` has its own `organization_id` column (the common case).
- `JoinedWidget` has no `organization_id` column of its own and is scoped
  via a join through `WidgetParent.organization_id` (proving the
  `scope_path` join-based case ahead of Work Unit 5's `ContractRepository`,
  which scopes the same way through `properties.organization_id`).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import UUID as SAUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.base import ScopedRepository


class _FixtureBase(DeclarativeBase):
    """Isolated declarative base for test-only fixture models — kept
    separate from `app.models.base.Base` so these throwaway tables never
    register on the real application metadata."""


class WidgetParent(_FixtureBase):
    __tablename__ = "fixture_widget_parents"

    id: Mapped[uuid.UUID] = mapped_column(
        SAUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        SAUUID(as_uuid=True), nullable=False
    )


class DirectWidget(_FixtureBase):
    __tablename__ = "fixture_direct_widgets"

    id: Mapped[uuid.UUID] = mapped_column(
        SAUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        SAUUID(as_uuid=True), nullable=False
    )
    name: Mapped[str | None] = mapped_column(nullable=True)


class JoinedWidget(_FixtureBase):
    __tablename__ = "fixture_joined_widgets"

    id: Mapped[uuid.UUID] = mapped_column(
        SAUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(SAUUID(as_uuid=True), nullable=False)


class DirectWidgetRepository(ScopedRepository[DirectWidget]):
    model = DirectWidget


class JoinedWidgetRepository(ScopedRepository[JoinedWidget]):
    model = JoinedWidget
    scope_path = ((WidgetParent, "parent_id"),)


def _scope(**overrides) -> OrgScope:
    defaults = dict(
        organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.OWNER
    )
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


# --- __init_subclass__ guard --------------------------------------------


def test_subclass_without_org_column_or_scope_path_raises_at_class_creation() -> None:
    with pytest.raises(TypeError):

        class UnscopedWidgetRepository(ScopedRepository[JoinedWidget]):
            model = JoinedWidget  # no organization_id column, no scope_path


# --- scope_clause() / select() — direct (own organization_id column) ----


def test_scope_clause_direct_filters_by_organization_id() -> None:
    scope = _scope()
    repo = DirectWidgetRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "fixture_direct_widgets" in sql
    assert scope.organization_id.hex in sql


def test_select_direct_only_returns_rows_in_scope_shape() -> None:
    scope = _scope()
    repo = DirectWidgetRepository(AsyncMock(), scope)

    stmt = repo.select()

    assert stmt.get_final_froms()[0].name == "fixture_direct_widgets"


# --- scope_clause() / select() — join path (no own organization_id) -----


def test_scope_clause_join_path_uses_exists_through_parent() -> None:
    scope = _scope()
    repo = JoinedWidgetRepository(AsyncMock(), scope)

    sql = _compiled(repo.select())

    assert "EXISTS" in sql
    assert "fixture_widget_parents" in sql
    assert scope.organization_id.hex in sql


# --- get_or_404 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_404_returns_the_row_when_found() -> None:
    fake_row = MagicMock(spec=DirectWidget)
    session = _session_returning(fake_row)
    repo = DirectWidgetRepository(session, _scope())

    result = await repo.get_or_404(uuid.uuid4())

    assert result is fake_row


@pytest.mark.asyncio
async def test_get_or_404_raises_not_found_error_when_missing() -> None:
    session = _session_returning(None)
    repo = DirectWidgetRepository(session, _scope())
    missing_id = uuid.uuid4()

    with pytest.raises(NotFoundError) as exc_info:
        await repo.get_or_404(missing_id)

    assert exc_info.value.id == missing_id
    assert exc_info.value.name == "DirectWidget"


@pytest.mark.asyncio
async def test_get_or_404_with_for_update_locks_the_row() -> None:
    session = _session_returning(MagicMock(spec=DirectWidget))
    repo = DirectWidgetRepository(session, _scope())

    await repo.get_or_404(uuid.uuid4(), for_update=True)

    stmt = session.execute.call_args.args[0]
    assert stmt._for_update_arg is not None


# --- add() -----------------------------------------------------------------


def test_add_direct_forces_scope_organization_id_discarding_caller_value() -> None:
    scope = _scope()
    session = MagicMock()
    repo = DirectWidgetRepository(session, scope)
    wrong_org_id = uuid.uuid4()

    widget = repo.add(name="lamp", organization_id=wrong_org_id)

    assert widget.organization_id == scope.organization_id
    assert widget.organization_id != wrong_org_id
    session.add.assert_called_once_with(widget)


def test_add_join_path_does_not_set_organization_id() -> None:
    scope = _scope()
    session = MagicMock()
    repo = JoinedWidgetRepository(session, scope)

    widget = repo.add(parent_id=uuid.uuid4())

    assert not hasattr(widget, "organization_id")
    session.add.assert_called_once_with(widget)


# --- update() ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_strips_organization_id_and_sets_remaining_fields() -> None:
    existing = DirectWidget(id=uuid.uuid4(), organization_id=uuid.uuid4(), name="old")
    session = _session_returning(existing)
    repo = DirectWidgetRepository(session, _scope())
    hijack_org_id = uuid.uuid4()
    original_org_id = existing.organization_id

    updated = await repo.update(existing.id, name="new", organization_id=hijack_org_id)

    assert updated.name == "new"
    assert updated.organization_id == original_org_id


@pytest.mark.asyncio
async def test_update_raises_not_found_when_row_out_of_scope() -> None:
    session = _session_returning(None)
    repo = DirectWidgetRepository(session, _scope())

    with pytest.raises(NotFoundError):
        await repo.update(uuid.uuid4(), name="new")


# --- delete() ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_deletes_the_row_when_in_scope() -> None:
    existing = MagicMock(spec=DirectWidget)
    session = _session_returning(existing)
    repo = DirectWidgetRepository(session, _scope())

    await repo.delete(uuid.uuid4())

    session.delete.assert_awaited_once_with(existing)


@pytest.mark.asyncio
async def test_delete_raises_not_found_when_row_out_of_scope() -> None:
    session = _session_returning(None)
    repo = DirectWidgetRepository(session, _scope())

    with pytest.raises(NotFoundError):
        await repo.delete(uuid.uuid4())
