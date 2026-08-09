"""Alembic migration environment.

Uses a SYNCHRONOUS SQLAlchemy engine (psycopg), derived from the app's
async `database_url` setting via `app.core.db.to_sync_url`, because
Alembic's migration runner requires a synchronous engine while the
application runtime uses an async one (asyncpg).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.core.db import to_sync_url
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_sync_url() -> str:
    """Resolve the synchronous database URL used by Alembic.

    Prefers an explicit `sqlalchemy.url` set on the Alembic config (e.g. via
    `-x` or ini overrides); otherwise derives it from the app's settings.
    """
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return to_sync_url(get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (renders SQL, no DB connection)."""
    url = get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (opens a real DB connection)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
