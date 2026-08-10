"""Application settings, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the application.

    Values are read from environment variables (or a local `.env` file
    during development). Sensible defaults are provided for local dev via
    docker-compose; production deployments must override them explicitly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Real Estate Helpdesk API"
    environment: str = "development"

    database_url: str = (
        "postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk"
    )

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"

    smtp_host: str = "localhost"
    smtp_port: int = 1025

    frontend_url: str = "http://localhost:3000"

    # Consumed once by `python -m app.core.seed` to bootstrap the single
    # platform-level super-admin (role=admin, organization_id=NULL). There
    # is no API endpoint that creates a super-admin; this is the only way.
    super_admin_email: str | None = None
    super_admin_password: str | None = None
    super_admin_name: str = "Super Admin"


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()
