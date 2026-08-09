"""FastAPI application entrypoint / app factory."""

from fastapi import FastAPI

from app.api.v1.auth import router as auth_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(title=settings.app_name)
    application.include_router(auth_router)
    return application


app = create_app()
