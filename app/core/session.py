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
    """FastAPI dependency yielding a request-scoped `AsyncSession`."""
    async with _session_factory()() as session:
        yield session
