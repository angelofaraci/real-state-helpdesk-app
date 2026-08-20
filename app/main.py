"""FastAPI application entrypoint / app factory."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.auth import router as auth_router
from app.api.v1.categories import router as categories_router
from app.api.v1.chat import router as chat_router
from app.api.v1.contracts import router as contracts_router
from app.api.v1.knowledge_base import router as knowledge_base_router
from app.api.v1.organizations import router as organizations_router
from app.api.v1.properties import router as properties_router
from app.api.v1.tickets import router as tickets_router
from app.api.v1.urgency_levels import router as urgency_levels_router
from app.api.v1.users import router as users_router
from app.core.chat_token import InvalidChatSessionTokenError
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.services.chat_rate_limit import ChatRateLimitExceededError


async def _handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    """Map every `NotFoundError` (row missing, or out of the caller's
    `OrgScope`) to a generic 404. The internal exception message — which
    names the model and id — is deliberately never included in the
    response, so an out-of-scope lookup cannot be used to probe another
    organization's data."""
    return JSONResponse(status_code=404, content={"detail": "Not found"})


async def _handle_chat_rate_limit_exceeded(
    request: Request, exc: ChatRateLimitExceededError
) -> JSONResponse:
    """Map every `ChatRateLimitExceededError` (stage 4 — chatbot) to 429,
    for any code path that raises it outside `app.api.v1.chat`'s own
    explicit `try`/`except` (defense in depth, same rationale as the
    `NotFoundError` global handler)."""
    return JSONResponse(status_code=429, content={"detail": str(exc)})


async def _handle_invalid_chat_session_token(
    request: Request, exc: InvalidChatSessionTokenError
) -> JSONResponse:
    """Map every `InvalidChatSessionTokenError` (stage 4 — chatbot) to
    401, for any code path that raises it outside
    `app.api.deps_chat.get_chat_session_scope`'s own explicit
    `try`/`except`."""
    return JSONResponse(status_code=401, content={"detail": "could not validate credentials"})


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(title=settings.app_name)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(organizations_router)
    application.include_router(properties_router)
    application.include_router(contracts_router)
    application.include_router(categories_router)
    application.include_router(urgency_levels_router)
    application.include_router(tickets_router)
    application.include_router(knowledge_base_router)
    application.include_router(chat_router)
    application.add_exception_handler(NotFoundError, _handle_not_found)
    application.add_exception_handler(
        ChatRateLimitExceededError, _handle_chat_rate_limit_exceeded
    )
    application.add_exception_handler(
        InvalidChatSessionTokenError, _handle_invalid_chat_session_token
    )
    return application


app = create_app()
