"""Chat-widget endpoints (stage 4 — chatbot, PR5 — API wiring).

Three routes, none behind `app.api.deps.get_principal`/`require_org_member`
— a chat-widget visitor is frequently anonymous (no `users` row at all), so
this router authenticates its own way: `POST /sessions` accepts a public
`widget_key` (optionally paired with a Bearer token for an identified
session); the two turn-taking routes (`POST`/`GET .../messages`) depend on
`app.api.deps_chat.get_chat_session_scope`, which resolves either a Bearer
JWT or the `X-Chat-Session` token minted by `POST /sessions` — see that
module's docstring for the full resolution order.

`app.core.exceptions.NotFoundError` is handled globally (see `app.main`),
so a cross-org/nonexistent `session_id` on the two turn-taking routes needs
no explicit `except` here — it is raised inside
`get_chat_session_scope`/`ChatSessionRepository.get_or_404` and reaches the
global 404 handler before this router's own code ever runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import openai
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_chat import (
    ChatPrincipal,
    _extract_bearer_token,
    _resolve_bearer_scope,
    get_chat_session_scope,
    get_redis,
)
from app.api.deps_rag import get_llm_client, get_rag_embedder
from app.core.chat_token import encode_chat_session_token
from app.core.config import get_settings
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import ChatMessageRole, ChatSessionStatus, UserRole
from app.models.organization import Organization
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.chat_session_repository import ChatSessionRepository
from app.schemas.chat import (
    ChatMessageHistoryItem,
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionCreateRequest,
    ChatSessionCreateResponse,
)
from app.services import chat as chat_service
from app.services.chat_rate_limit import ChatRateLimitExceededError, check_and_increment
from app.services.rag_embeddings import RagEmbeddingProvider

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


async def _resolve_optional_user_id(
    authorization: str | None, session: AsyncSession
) -> UUID | None:
    """`user_id` of the identified caller starting this session, or `None`
    for an anonymous widget visitor. An ABSENT or INVALID Bearer token both
    resolve to `None` (anonymous) here — unlike `get_chat_session_scope`,
    an invalid Bearer token at session-start time is not itself an error:
    the visitor simply gets an anonymous session instead."""
    token = _extract_bearer_token(authorization)
    if token is None:
        return None
    try:
        scope = await _resolve_bearer_scope(token, session)
    except HTTPException:
        return None
    return scope.user_id


@router.post(
    "/sessions", response_model=ChatSessionCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_chat_session(
    payload: ChatSessionCreateRequest,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ChatSessionCreateResponse:
    """Start a new chat session for the organization identified by
    `widget_key` (404 if unrecognized). If a valid Bearer access token is
    ALSO present, the session is identified (`user_id` set); otherwise it
    is anonymous."""
    org_stmt = select(Organization).where(Organization.chat_widget_key == payload.widget_key)
    org = (await session.execute(org_stmt)).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    user_id = await _resolve_optional_user_id(authorization, session)
    scope = (
        OrgScope(organization_id=org.id, user_id=user_id, role=UserRole.TENANT)
        if user_id is not None
        else OrgScope.for_anonymous_chat(org.id)
    )

    chat_session = ChatSessionRepository(session, scope).add(
        user_id=user_id, status=ChatSessionStatus.ACTIVE
    )
    await session.flush()

    settings = get_settings()
    chat_token = encode_chat_session_token(
        session_id=chat_session.id, organization_id=org.id, user_id=user_id
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.chat_session_token_ttl_minutes)

    return ChatSessionCreateResponse(
        id=chat_session.id,
        status=chat_session.status,
        is_anonymous=user_id is None,
        chat_token=chat_token,
        expires_at=expires_at,
    )


@router.post("/sessions/{session_id}/messages", response_model=ChatMessageResponse)
async def post_chat_message(
    session_id: UUID,
    payload: ChatMessageRequest,
    principal: ChatPrincipal = Depends(get_chat_session_scope),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
    embedder: RagEmbeddingProvider = Depends(get_rag_embedder),
    llm_client: openai.AsyncOpenAI = Depends(get_llm_client),
) -> ChatMessageResponse:
    """Send one visitor turn. Rate-limited (429) after scope resolution and
    BEFORE persisting the message or calling the LLM/embedder; 409 if the
    session is no longer ACTIVE."""
    chat_session = principal.chat_session

    if chat_session.status != ChatSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="chat session is not active"
        )

    try:
        await check_and_increment(redis, chat_session_id=chat_session.id)
    except ChatRateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc

    result = await chat_service.send_message(
        session,
        scope=principal.scope,
        chat_session=chat_session,
        content=payload.content,
        embedder=embedder,
        llm_client=llm_client,
    )

    return ChatMessageResponse(
        reply=result.reply,
        session_status=chat_session.status,
        escalated=result.escalated,
        ticket_id=result.ticket_id,
        tools_used=result.tools_used,
    )


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageHistoryItem])
async def get_chat_messages(
    session_id: UUID,
    principal: ChatPrincipal = Depends(get_chat_session_scope),
    session: AsyncSession = Depends(get_session),
) -> list[ChatMessageHistoryItem]:
    """Chronological message history for this session, excluding `tool`
    role rows (raw tool results are never user-facing — see
    `app.services.chat_tools._copy_chat_messages_to_ticket`'s docstring for
    the same exclusion rule applied elsewhere)."""
    stmt = ChatMessageRepository(session, principal.scope).list_for_session(session_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        ChatMessageHistoryItem.model_validate(row)
        for row in rows
        if row.role != ChatMessageRole.TOOL
    ]
