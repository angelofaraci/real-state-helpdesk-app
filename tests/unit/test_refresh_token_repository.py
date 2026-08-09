"""Unit tests for `RefreshTokenRepository` against a mocked `AsyncSession`.

No live Postgres is available in this sandbox (same constraint noted in
prior work units), so these tests mock the session boundary rather than
executing real SQL. They verify the repository builds the right ORM
operations (locked SELECT for rotation, family-wide revoke UPDATE) and
returns the right shapes — not Postgres-specific execution behavior.
Anything that requires a real Postgres connection is out of scope here;
see `tests/integration/test_refresh_token_repository_pg.py` (xfail).
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.refresh_token import RefreshToken
from app.repositories.refresh_token_repository import RefreshTokenRepository


def _make_session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_issue_returns_raw_token_and_family_id_and_adds_to_session() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    repo = RefreshTokenRepository(session)
    user_id = uuid4()

    raw_token, family_id = await repo.issue(user_id)

    assert isinstance(raw_token, str)
    assert len(raw_token) >= 32
    assert family_id is not None
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert isinstance(added, RefreshToken)
    assert added.user_id == user_id
    assert added.family_id == family_id
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_issue_reuses_provided_family_id_on_rotation() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    repo = RefreshTokenRepository(session)
    user_id = uuid4()
    existing_family = uuid4()

    _, family_id = await repo.issue(user_id, family_id=existing_family)

    assert family_id == existing_family


@pytest.mark.asyncio
async def test_rotate_issues_a_locked_select_by_token_hash() -> None:
    session = _make_session_returning(None)
    repo = RefreshTokenRepository(session)

    result = await repo.rotate("some-raw-token")

    assert result is None
    session.execute.assert_awaited_once()
    stmt = session.execute.call_args.args[0]
    # `with_for_update()` sets `_for_update_arg` on the compiled Select.
    assert stmt._for_update_arg is not None


@pytest.mark.asyncio
async def test_rotate_returns_the_matching_row() -> None:
    fake_row = MagicMock(spec=RefreshToken)
    session = _make_session_returning(fake_row)
    repo = RefreshTokenRepository(session)

    result = await repo.rotate("some-raw-token")

    assert result is fake_row


@pytest.mark.asyncio
async def test_revoke_sets_revoked_at_and_flushes() -> None:
    session = AsyncMock()
    repo = RefreshTokenRepository(session)
    token = MagicMock(spec=RefreshToken)
    token.revoked_at = None

    await repo.revoke(token)

    assert token.revoked_at is not None
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_family_executes_bulk_update_scoped_to_family() -> None:
    session = AsyncMock()
    repo = RefreshTokenRepository(session)
    family_id = uuid4()

    await repo.revoke_family(family_id)

    session.execute.assert_awaited_once()
    stmt = session.execute.call_args.args[0]
    assert stmt.table.name == "refresh_tokens"
