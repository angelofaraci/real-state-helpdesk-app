"""FastAPI application entrypoint / app factory."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.auth import router as auth_router
from app.api.v1.contracts import router as contracts_router
from app.api.v1.organizations import router as organizations_router
from app.api.v1.properties import router as properties_router
from app.api.v1.users import router as users_router
from app.core.config import get_settings
from app.core.exceptions import NotFoundError


async def _handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    """Map every `NotFoundError` (row missing, or out of the caller's
    `OrgScope`) to a generic 404. The internal exception message — which
    names the model and id — is deliberately never included in the
    response, so an out-of-scope lookup cannot be used to probe another
    organization's data."""
    return JSONResponse(status_code=404, content={"detail": "Not found"})


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(title=settings.app_name)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(organizations_router)
    application.include_router(properties_router)
    application.include_router(contracts_router)
    application.add_exception_handler(NotFoundError, _handle_not_found)
    return application


app = create_app()
