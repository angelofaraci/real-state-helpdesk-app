"""FastAPI dependency providers for the chat-widget API (stage 4 —
chatbot, PR5 — API wiring).

`get_chat_session_scope` is the single entry point every chat route
(`app/api/v1/chat.py`) depends on to resolve BOTH the caller's `OrgScope`
AND the already-loaded `ChatSession` row it is acting on, from one of two
credential shapes:

- **Bearer JWT** (`Authorization: Bearer <access-token>`) — an authenticated
  app user (e.g. re-opening the widget while already logged in). Takes
  PRIORITY over any `X-Chat-Session` header when both are present: the
  `X-Chat-Session` token is not even decoded in that case. Resolved the
  exact same way `app.api.deps.get_principal` resolves a real access
  token (decode -> load `User` -> `OrgScope.from_principal`), just written
  by hand here rather than via `Depends(get_principal)`, since
  `HTTPBearer`'s automatic "403 if missing" behavior doesn't fit this
  route's optional-Bearer, X-Chat-Session-header fallback shape.
- **`X-Chat-Session` header** — the chat-session token minted by
  `POST /api/v1/chat/sessions` (`app.core.chat_token.encode_chat_session_token`),
  used by both anonymous widget visitors (no `users` row at all) and an
  identified visitor who chooses not to resend their Bearer token on every
  chat turn. Its `sid` claim MUST equal the `session_id` path parameter —
  a token minted for one session can never be replayed against another,
  even within the same org (401, not 404, since this is a credential
  mismatch, not a missing-resource lookup).

Either way, once a scope is resolved the `chat_sessions` row is loaded via
`ChatSessionRepository.get_or_404` under that exact scope — a session that
exists but belongs to a DIFFERENT organization than the resolved scope's
surfaces as 404 (never 403), the same existence-hiding rule every other
cross-org lookup in this app follows (see `app.core.exceptions.NotFoundError`
and `app.main`'s global handler).

An identified `X-Chat-Session` token (`uid` claim set) carries no `role`
claim of its own (the chat-session token's claim set is intentionally
narrow — see `app.core.chat_token`'s docstring), so its scope is built with
`UserRole.TENANT`, the least-privileged role: a chat-widget visitor is a
narrower context than a full authenticated request, and TENANT is the
correct default steady-state role for someone chatting through a
`chat_session`-token-only credential (an agent/admin would ordinarily still
be carrying — and using — their real Bearer access token instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chat_token import InvalidChatSessionTokenError, decode_chat_session_token
from app.core.config import get_settings
from app.core.jwt import InvalidAccessTokenError, decode_access_token
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.chat_session import ChatSession
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.repositories.chat_session_repository import ChatSessionRepository

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="could not validate credentials",
)


@dataclass(frozen=True, slots=True)
class ChatPrincipal:
    """The resolved caller of a chat-widget request: its `OrgScope`, the
    already-loaded `ChatSession` row it is acting on, and whether the
    caller is an anonymous widget visitor (no `users` row at all)."""

    scope: OrgScope
    chat_session: ChatSession
    is_anonymous: bool


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def _resolve_bearer_scope(token: str, session: AsyncSession) -> OrgScope:
    try:
        claims = decode_access_token(token)
    except InvalidAccessTokenError as exc:
        raise _UNAUTHORIZED from exc

    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise _UNAUTHORIZED from exc

    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or user.status != UserStatus.ACTIVE:
        raise _UNAUTHORIZED

    if user.organization_id is None:
        # A super-admin has no org scope, and therefore cannot act as a
        # chat-widget caller at all.
        raise _UNAUTHORIZED

    return OrgScope.from_principal(user)


def _resolve_chat_token_scope(token: str, session_id: UUID) -> tuple[OrgScope, bool]:
    try:
        claims = decode_chat_session_token(token)
    except InvalidChatSessionTokenError as exc:
        raise _UNAUTHORIZED from exc

    if claims.sid != str(session_id):
        raise _UNAUTHORIZED

    try:
        organization_id = UUID(claims.org)
    except (TypeError, ValueError) as exc:
        raise _UNAUTHORIZED from exc

    if claims.uid is None:
        return OrgScope.for_anonymous_chat(organization_id), True

    try:
        user_id = UUID(claims.uid)
    except (TypeError, ValueError) as exc:
        raise _UNAUTHORIZED from exc

    return (
        OrgScope(organization_id=organization_id, user_id=user_id, role=UserRole.TENANT),
        False,
    )


async def get_chat_session_scope(
    session_id: UUID,
    chat_token: str | None = Header(default=None, alias="X-Chat-Session"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ChatPrincipal:
    """Resolve the caller's `OrgScope` and load the `chat_sessions` row
    named by `session_id` under it — see module docstring for the full
    Bearer-vs-`X-Chat-Session` resolution order."""
    bearer_token = _extract_bearer_token(authorization)
    if bearer_token is not None:
        scope = await _resolve_bearer_scope(bearer_token, session)
        is_anonymous = False
    elif chat_token is not None:
        scope, is_anonymous = _resolve_chat_token_scope(chat_token, session_id)
    else:
        raise _UNAUTHORIZED

    chat_session = await ChatSessionRepository(session, scope).get_or_404(session_id)
    return ChatPrincipal(scope=scope, chat_session=chat_session, is_anonymous=is_anonymous)


async def get_redis():
    """Return an arq-pooled Redis client for the chat rate limiter.

    Reuses `arq`'s bundled `redis.asyncio` client via the exact same
    `create_pool(RedisSettings.from_dsn(settings.redis_url))` pattern
    `app.workers.rag`/`app.workers.classification` already use for the job
    queue — `redis` is deliberately NOT an added pyproject dependency of
    its own (see `app.services.chat_rate_limit`'s module docstring); the
    object `create_pool` returns already satisfies `chat_rate_limit`'s
    duck-typed `pipeline()`/`incr`/`expire`/`execute` protocol, since arq's
    `ArqRedis` subclasses `redis.asyncio.Redis` directly.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = get_settings()
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))
