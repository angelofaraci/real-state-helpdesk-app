"""Database URL helpers shared by the app runtime and Alembic migrations."""


def to_sync_url(database_url: str) -> str:
    """Derive a synchronous SQLAlchemy URL from an async `database_url`.

    The application uses `postgresql+asyncpg://...` at runtime, but Alembic's
    migration engine must be synchronous. This performs a driver substitution
    (`asyncpg` -> `psycopg`) while preserving every other URL component.
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
