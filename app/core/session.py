"""Async SQLAlchemy engine + `AsyncSession` FastAPI dependency for the app
runtime (as opposed to `tests/conftest.py`'s isolated test-transaction
fixture, which mirrors this shape but rolls back after every test).
"""

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def build_engine() -> AsyncEngine:
    """Construct the async SQLAlchemy engine used by the running app.

    Building an async engine performs no I/O: connections are opened
    lazily on first use.
    """
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def _engine() -> AsyncEngine:
    return build_engine()


@lru_cache
def _session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped `AsyncSession`.

    Commits the session once the request handler returns without raising,
    so writes made through this dependency are durable. If the handler
    raises an `Exception`, the session is rolled back instead and the
    exception is re-raised untouched. `GeneratorExit` (raised by `aclose()`
    on client disconnect/cancellation) is intentionally NOT caught here —
    only `Exception` subtypes are — so cancellation propagates uncaught
    instead of being mistaken for an application error.
    """
    async with _session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
