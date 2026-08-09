"""Unit tests for the shared test-suite fixtures in `tests/conftest.py`.

These only verify that the engine/session factory construct correctly from
the config's dev-default `DATABASE_URL` — they never open a real connection,
since building an async SQLAlchemy engine is lazy (no I/O until first use).
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import build_engine


def test_build_engine_returns_async_engine_without_connecting() -> None:
    engine = build_engine()

    assert isinstance(engine, AsyncEngine)


def test_build_engine_uses_the_configured_database_url() -> None:
    from app.core.config import get_settings

    engine = build_engine()

    assert engine.url.render_as_string(hide_password=False) == get_settings().database_url
    # The engine's driver must be asyncpg (the app's runtime driver), not the
    # sync psycopg driver used by Alembic.
    assert engine.url.drivername == "postgresql+asyncpg"
