"""Unit tests for the app runtime's async engine/session wiring.

Building the engine and the session-factory-backed dependency performs no
I/O (SQLAlchemy async engines connect lazily), so this is safe to test
without a live Postgres instance.

The commit/rollback contract of `get_session()` is exercised against a
mocked `AsyncSession` (via a fake async-context-manager `_session_factory`),
driving the async generator manually with `asend`/`athrow`/`aclose` to
simulate exactly how FastAPI drives dependency generators: normal
completion, an exception raised by the route handler, and a client
disconnect (`GeneratorExit`, raised by `aclose()`).
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.session import build_engine, get_session


def test_build_engine_returns_an_async_engine() -> None:
    engine = build_engine()
    assert isinstance(engine, AsyncEngine)


def test_get_session_is_an_async_generator_dependency() -> None:
    generator = get_session()
    assert isinstance(generator, AsyncGenerator)


class _FakeSessionContextManager:
    """Stand-in for `_session_factory()()` — an `AsyncSession` used as an
    async context manager. Mirrors real SQLAlchemy's `__aenter__` (returns
    the session) / `__aexit__` (does not suppress exceptions).
    """

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _fake_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.mark.asyncio
async def test_generator_completes_normally_commits_once_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _fake_session()
    monkeypatch.setattr(
        "app.core.session._session_factory",
        MagicMock(return_value=MagicMock(return_value=_FakeSessionContextManager(session))),
    )

    async for yielded in get_session():
        assert yielded is session

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_exception_from_consumer_rolls_back_and_reraises_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _fake_session()
    monkeypatch.setattr(
        "app.core.session._session_factory",
        MagicMock(return_value=MagicMock(return_value=_FakeSessionContextManager(session))),
    )

    agen = get_session()
    yielded = await agen.asend(None)
    assert yielded is session

    class _RouteHandlerError(Exception):
        pass

    with pytest.raises(_RouteHandlerError):
        await agen.athrow(_RouteHandlerError("boom"))

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_generator_exit_on_client_disconnect_propagates_uncaught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _fake_session()
    monkeypatch.setattr(
        "app.core.session._session_factory",
        MagicMock(return_value=MagicMock(return_value=_FakeSessionContextManager(session))),
    )

    agen = get_session()
    yielded = await agen.asend(None)
    assert yielded is session

    # `aclose()` throws `GeneratorExit` into the generator at the `yield`
    # point. Because `GeneratorExit` is a `BaseException`, not an
    # `Exception`, `except Exception` must NOT catch it — it must propagate
    # out of the `async with` block uncaught, and `aclose()` must complete
    # without raising (the normal contract when a generator does not
    # suppress `GeneratorExit`).
    await agen.aclose()

    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
