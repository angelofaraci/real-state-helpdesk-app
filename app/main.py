"""FastAPI application entrypoint / app factory."""

from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(title=settings.app_name)
    return application


app = create_app()
