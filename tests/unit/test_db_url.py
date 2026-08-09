"""Unit tests for the async->sync database URL derivation helper.

Alembic's migration runner uses a synchronous SQLAlchemy engine (psycopg),
while the application runtime uses an async engine (asyncpg). This module
tests the pure function that derives the sync URL from the app's
`asyncpg`-based `database_url` setting by driver substitution.
"""

from app.core.db import to_sync_url


def test_to_sync_url_replaces_asyncpg_driver_with_psycopg() -> None:
    async_url = "postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk"

    result = to_sync_url(async_url)

    assert result == "postgresql+psycopg://helpdesk:helpdesk@localhost:5432/helpdesk"


def test_to_sync_url_preserves_query_string_and_different_host() -> None:
    async_url = "postgresql+asyncpg://u:p@db.internal:5433/mydb?sslmode=require"

    result = to_sync_url(async_url)

    assert result == "postgresql+psycopg://u:p@db.internal:5433/mydb?sslmode=require"
