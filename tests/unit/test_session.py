"""Unit tests for the app runtime's async engine/session wiring.

Building the engine and the session-factory-backed dependency performs no
I/O (SQLAlchemy async engines connect lazily), so this is safe to test
without a live Postgres instance.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.session import build_engine, get_session


def test_build_engine_returns_an_async_engine() -> None:
    engine = build_engine()
    assert isinstance(engine, AsyncEngine)


def test_get_session_is_an_async_generator_dependency() -> None:
    generator = get_session()
    assert isinstance(generator, AsyncGenerator)
