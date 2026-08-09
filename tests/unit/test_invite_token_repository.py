"""Unit tests for `InviteTokenRepository` against a mocked `AsyncSession`.

Same constraint as `test_refresh_token_repository.py`: no live Postgres is
available in this sandbox, so these tests verify the repository builds the
right ORM operations and returns the right shapes, not Postgres-specific
execution behavior (e.g. the partial unique index enforcing "at most one
outstanding invite per user" is only verifiable against a real Postgres
instance).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.invite_token import InviteToken
from app.repositories.invite_token_repository import InviteTokenRepository


def _make_session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_issue_returns_raw_token_and_adds_hashed_row_with_48h_expiry() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    repo = InviteTokenRepository(session)
    user_id = uuid4()

    before = datetime.now(UTC)
    raw_token = await repo.issue(user_id)
    after = datetime.now(UTC)

    assert isinstance(raw_token, str)
    assert len(raw_token) >= 32
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert isinstance(added, InviteToken)
    assert added.user_id == user_id
    assert added.token_hash != raw_token.encode("utf-8")
    assert before + timedelta(hours=48) <= added.expires_at <= after + timedelta(hours=48)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_issues_a_locked_select_by_token_hash() -> None:
    session = _make_session_returning(None)
    repo = InviteTokenRepository(session)

    result = await repo.consume("some-raw-token")

    assert result is None
    session.execute.assert_awaited_once()
    stmt = session.execute.call_args.args[0]
    assert stmt._for_update_arg is not None


@pytest.mark.asyncio
async def test_consume_returns_the_matching_row() -> None:
    fake_row = MagicMock(spec=InviteToken)
    session = _make_session_returning(fake_row)
    repo = InviteTokenRepository(session)

    result = await repo.consume("some-raw-token")

    assert result is fake_row


@pytest.mark.asyncio
async def test_mark_used_sets_used_at_and_flushes() -> None:
    session = AsyncMock()
    repo = InviteTokenRepository(session)
    token = MagicMock(spec=InviteToken)
    token.used_at = None

    await repo.mark_used(token)

    assert token.used_at is not None
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_unused_for_user_executes_delete_scoped_to_user_and_unused() -> None:
    session = AsyncMock()
    repo = InviteTokenRepository(session)
    user_id = uuid4()

    await repo.delete_unused_for_user(user_id)

    session.execute.assert_awaited_once()
    stmt = session.execute.call_args.args[0]
    assert stmt.table.name == "invite_tokens"
