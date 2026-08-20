"""Shared pytest fixtures for the test suite.

Provides an async SQLAlchemy engine/session fixture set using a
SAVEPOINT-based per-test transaction rollback pattern: each test runs inside
an outer transaction plus a nested SAVEPOINT. Both are rolled back after the
test, so tests never leave data behind in the shared Compose Postgres
instance, even if application code under test calls `session.commit()`.

`get_session` / `get_principal` FastAPI dependency overrides do NOT exist
yet — they are introduced by a later work unit (auth/session wiring). The
`db_session` fixture below is deliberately shaped so that a later work unit
can plug them in via, e.g.:

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_principal] = lambda: some_test_principal

without needing to reshape this fixture.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def build_engine() -> AsyncEngine:
    """Construct the async SQLAlchemy engine used by the test suite.

    Building an async engine performs no I/O: connections are opened lazily
    on first use, so this is safe to call without a live database.
    """
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Function-scoped async engine, built fresh per test.

    Originally session-scoped, but pytest-asyncio's default fixture loop
    scope is "function" (see `pyproject.toml`'s
    `asyncio_default_fixture_loop_scope`), and a session-scoped async
    fixture cannot depend on a function-scoped `event_loop` — every test
    using it would fail at setup with a `ScopeMismatch` error before ever
    reaching a real database. This was never caught earlier because no
    test actually used this fixture until stage 4's real-Postgres
    integration tests (see `tests/integration/test_chat_flow.py`)."""
    eng = build_engine()
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test `AsyncSession` isolated via an outer transaction + SAVEPOINT.

    Adapted from SQLAlchemy's documented "join a Session into an external
    transaction" recipe for test suites: the outer connection-level
    transaction is never committed, and a SAVEPOINT is restarted every time
    application code ends its own (sub-)transaction, so `session.commit()`
    calls made by code under test do not escape the outer rollback.
    """
    connection: AsyncConnection = await engine.connect()
    await connection.begin()

    session_factory = async_sessionmaker(bind=connection, expire_on_commit=False)
    session = session_factory()

    nested = await connection.begin_nested()

    @event.listens_for(session.sync_session, "after_transaction_end")
    def _restart_savepoint(sync_session, transaction) -> None:  # noqa: ANN001
        nonlocal nested
        if not nested.is_active:
            nested = connection.sync_connection.begin_nested()

    try:
        yield session
    finally:
        await session.close()
        await connection.rollback()
        await connection.close()
