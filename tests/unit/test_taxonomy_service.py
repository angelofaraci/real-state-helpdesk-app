"""Unit tests for `app.services.taxonomy_service.delete_or_deactivate` — the
shared delete-or-deactivate helper used by both `category_service` and
`urgency_level_service`.

No live Postgres is available in this sandbox, so the SAVEPOINT/`IntegrityError`
path is simulated against a mocked `AsyncSession`: `session.begin_nested()`
is stubbed with a real async-context-manager stand-in (so the exception it
raises inside the `async with` block propagates exactly like SQLAlchemy's
real nested-transaction context manager — it must NOT be swallowed by a
naive mock whose `__aexit__` returns a truthy value), and `session.flush()`
is scripted per-scenario to succeed or raise `IntegrityError` on the delete
attempt specifically.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import NotFoundError
from app.services.taxonomy_service import TaxonomyDeleteResult, delete_or_deactivate


class _NestedTransaction:
    """Stand-in for the object returned by `AsyncSession.begin_nested()`.

    Mirrors real SQLAlchemy behavior: on a clean exit nothing special
    happens; on an exception inside the `with` block, `__aexit__` returns
    `False` so the exception propagates to the caller (real SQLAlchemy
    rolls back to the savepoint and re-raises) instead of being silently
    swallowed, which is what a bare `MagicMock`/`AsyncMock` would do by
    default (its `__aexit__` return value is truthy).
    """

    async def __aenter__(self) -> "_NestedTransaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _instance(**overrides) -> MagicMock:
    defaults = dict(id=uuid4(), active=True)
    defaults.update(overrides)
    row = MagicMock()
    for key, value in defaults.items():
        setattr(row, key, value)
    return row


def _repo_returning(instance) -> AsyncMock:
    repo = AsyncMock()
    repo.get_or_404.return_value = instance
    return repo


@pytest.mark.asyncio
async def test_unreferenced_row_hard_deletes() -> None:
    instance = _instance(active=True)
    repo = _repo_returning(instance)
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=_NestedTransaction())
    session.flush = AsyncMock(return_value=None)

    result = await delete_or_deactivate(session, repo=repo, id_=instance.id)

    assert result == TaxonomyDeleteResult(id=instance.id, deleted=True, active=False)
    session.delete.assert_awaited_once_with(instance)


@pytest.mark.asyncio
async def test_referenced_row_soft_deactivates_and_still_returns_200_shape() -> None:
    instance = _instance(active=True)
    repo = _repo_returning(instance)
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=_NestedTransaction())
    # First flush (inside the SAVEPOINT, triggered by the delete attempt)
    # raises; the second flush (persisting active=False) succeeds.
    session.flush = AsyncMock(side_effect=[IntegrityError("stmt", {}, Exception("fk")), None])

    result = await delete_or_deactivate(session, repo=repo, id_=instance.id)

    assert result == TaxonomyDeleteResult(id=instance.id, deleted=False, active=False)
    assert instance.active is False


@pytest.mark.asyncio
async def test_already_inactive_and_now_unreferenced_row_still_hard_deletes() -> None:
    instance = _instance(active=False)
    repo = _repo_returning(instance)
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=_NestedTransaction())
    session.flush = AsyncMock(return_value=None)

    result = await delete_or_deactivate(session, repo=repo, id_=instance.id)

    assert result == TaxonomyDeleteResult(id=instance.id, deleted=True, active=False)


@pytest.mark.asyncio
async def test_already_inactive_and_still_referenced_row_is_a_no_op_returning_200_shape() -> None:
    instance = _instance(active=False)
    repo = _repo_returning(instance)
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=_NestedTransaction())
    session.flush = AsyncMock(side_effect=[IntegrityError("stmt", {}, Exception("fk")), None])

    result = await delete_or_deactivate(session, repo=repo, id_=instance.id)

    assert result == TaxonomyDeleteResult(id=instance.id, deleted=False, active=False)
    assert instance.active is False


@pytest.mark.asyncio
async def test_raises_not_found_for_a_missing_or_out_of_scope_row() -> None:
    repo = AsyncMock()
    repo.get_or_404.side_effect = NotFoundError("Category", uuid4())
    session = AsyncMock()

    with pytest.raises(NotFoundError):
        await delete_or_deactivate(session, repo=repo, id_=uuid4())
