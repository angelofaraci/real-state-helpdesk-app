"""Async arq jobs for stage-5 multichannel WhatsApp processing (PR5 —
WhatsApp worker + outbound; design ADR-10).

TWO separate jobs, deliberately never combined into one:

- `process_whatsapp_message`: runs the actual chatbot turn
  (`app.services.chat.send_message`) for one already-ingested inbound
  message and commits it, THEN enqueues `send_whatsapp_reply` to deliver
  the reply over the WhatsApp Cloud API.
- `send_whatsapp_reply`: sends (or, outside the 24h customer-service
  window, defers) that already-computed reply.

Splitting them means a transient failure/retry of the OUTBOUND HTTP leg
(`send_whatsapp_reply`) can never re-run the LLM turn — `process_
whatsapp_message` commits its work (the persisted chat turn, any
ticket/message writes from tool calls) BEFORE it ever enqueues the reply
job, so the two are fully decoupled once that commit lands.

Scope bootstrap: `process_whatsapp_message` receives only
`(chat_session_id, wamid, text_body)` — no `organization_id` — so it must
look the session up directly (bypassing `ChatSessionRepository`, which
requires an already-known `OrgScope`) to learn its `organization_id`/
`user_id` first. Once known, the session's owning `User` is loaded under
`OrgScope.for_background_worker(...)` (read-only bootstrap), and the
actual chat turn runs under `OrgScope.from_principal(user)` — WhatsApp
sessions always have a real (auto-provisioned) `user_id`, never anonymous
(see `app.services.whatsapp_ingestion`), so `from_principal` is always
valid here.

`ctx["rag_embedder"]` vs `ctx["embedder"]`: `chat.send_message`'s
`embedder=` kwarg MUST receive `ctx["rag_embedder"]` (the stage-3 RAG
provider `app.api.v1.chat.py` also injects) — never `ctx["embedder"]`
(the stage-2 classification provider, wrong model/dimension for chat
retrieval). Using the wrong one raises no exception; it just silently
returns garbage/empty RAG context. See `app.workers.settings`'s module
docstring.

Error handling mirrors `app.workers.rag`'s `_is_transient_error` split
(duplicated here rather than shared, matching every other worker module's
own local copy):

- Transient (network/timeout/rate-limit/5xx) -> `arq.Retry` with the same
  exponential-backoff `defer` convention as every other worker job in this
  codebase.
- Permanent (anything else, including "no chat session found for this id"
  and "no LLM client available") -> logged and swallowed; arq must not
  retry a permanent failure.

`send_whatsapp_reply` pre-checks the 24h customer-service window
(`chat_session.channel_metadata["last_inbound_at"]` vs
`settings.whatsapp_customer_window_hours`) before ever attempting a send,
and POST-checks Meta error code 131047 (the same re-engagement error,
surfaced only once the HTTP call is actually made) — either trigger routes
to the SAME deferred-reply path: appended to
`chat_session.channel_metadata["deferred_replies"]` (capped at 20 via
`channel_metadata.with_appended`), logged at WARNING with ids/reason only
(never the reply content), and returned without a retry — the window will
not reopen on its own, so retrying changes nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import openai
from arq import Retry, create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.chat_session import ChatSession
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.services import chat, whatsapp_client
from app.services.channel_metadata import with_appended
from app.services.llm_client import build_llm_client
from app.services.whatsapp_client import WhatsAppSendError

logger = logging.getLogger(__name__)

# Mirrors `app.workers.rag`'s own cap precedent for
# `channel_metadata`-backed dedup/bookkeeping lists.
_DEFERRED_REPLIES_CAP = 20

# Meta's error code for "more than 24 hours have passed since the customer
# last replied to this number" — the re-engagement/customer-service-window
# error. Surfaced only once the HTTP call is actually attempted (the
# pre-check above catches the common case; this is the belt-and-suspenders
# catch for a window that closes mid-flight).
_META_OUTSIDE_WINDOW_ERROR_CODE = 131047

_DEFERRED_REASON = "outside_24h_window"

_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    openai.APIConnectionError,
)


def _is_transient_error(exc: BaseException) -> bool:
    """True for a network-shaped/rate-limit/5xx failure that should be
    retried; False for anything that should permanently fail the job.
    Mirrors `app.workers.rag._is_transient_error` exactly."""
    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return True
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True
    return False


def _resolve_llm_client(ctx: dict[str, Any]) -> Any:
    """`ctx.get("llm_client")` when injected (tests); otherwise a real
    `openai.AsyncOpenAI` built from `settings.openai_api_key` — same
    injectable-client pattern as `app.workers.rag._summarize_resolution`
    and `app.services.llm_fallback.classify_with_llm`.

    Returns `None` when no client is injected AND no API key is
    configured — the caller treats that as a permanent failure (there is
    nothing to retry: retrying will not make a key appear)."""
    llm_client = ctx.get("llm_client")
    if llm_client is not None:
        return llm_client
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    return build_llm_client(feature="whatsapp", api_key=settings.openai_api_key)


async def process_whatsapp_message(
    ctx: dict[str, Any], chat_session_id: UUID, wamid: str | None, text_body: str | None
) -> None:
    """Run the chatbot turn for one already-ingested inbound WhatsApp
    message, commit it, then enqueue `send_whatsapp_reply` to deliver the
    reply. See module docstring for the full contract."""
    session_factory = ctx["session_factory"]
    rag_embedder = ctx["rag_embedder"]

    async with session_factory() as session:
        bootstrap_stmt = select(ChatSession).where(ChatSession.id == chat_session_id)
        chat_session = (await session.execute(bootstrap_stmt)).scalar_one_or_none()
        if chat_session is None:
            logger.warning(
                "process_whatsapp_message: chat session %s not found; skipping",
                chat_session_id,
            )
            return

        read_scope = OrgScope.for_background_worker(chat_session.organization_id)
        user = await UserRepository(session, read_scope).get_or_404(chat_session.user_id)
        scope = OrgScope.from_principal(user)

        llm_client = _resolve_llm_client(ctx)
        if llm_client is None:
            logger.warning(
                "process_whatsapp_message: no LLM client available for chat session %s; "
                "not retrying",
                chat_session_id,
            )
            return

        try:
            turn_result = await chat.send_message(
                session,
                scope=scope,
                chat_session=chat_session,
                content=text_body or "",
                embedder=rag_embedder,
                llm_client=llm_client,
            )
        except Exception as exc:
            if _is_transient_error(exc):
                job_try = ctx.get("job_try", 1)
                raise Retry(defer=2**job_try) from exc
            logger.warning(
                "permanent failure processing WhatsApp message for chat session %s",
                chat_session_id,
                exc_info=True,
            )
            return

        await session.commit()

    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("send_whatsapp_reply", chat_session_id, turn_result.reply)
    finally:
        await redis.aclose()


def _outside_customer_window(chat_session: ChatSession, *, window_hours: int) -> bool:
    last_inbound_at_raw = (chat_session.channel_metadata or {}).get("last_inbound_at")
    if not last_inbound_at_raw:
        return True
    try:
        last_inbound_at = datetime.fromisoformat(last_inbound_at_raw)
    except (TypeError, ValueError):
        return True
    elapsed_hours = (datetime.now(UTC) - last_inbound_at).total_seconds() / 3600
    return elapsed_hours > window_hours


def _record_deferred_reply(chat_session: ChatSession, *, content: str, reason: str) -> None:
    chat_session.channel_metadata = with_appended(
        chat_session.channel_metadata,
        "deferred_replies",
        {
            "deferred_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "content": content,
        },
        cap=_DEFERRED_REPLIES_CAP,
    )
    logger.warning(
        "WhatsApp reply deferred chat_session_id=%s organization_id=%s reason=%s",
        chat_session.id,
        chat_session.organization_id,
        reason,
    )


async def send_whatsapp_reply(
    ctx: dict[str, Any], chat_session_id: UUID, reply_content: str
) -> None:
    """Send `reply_content` over the WhatsApp Cloud API for `chat_session_
    id`, or persist it as a deferred reply if the 24h customer-service
    window has elapsed (pre-check) or Meta rejects the send with error
    code 131047 (post-check). See module docstring for the full contract.
    """
    session_factory = ctx["session_factory"]
    settings = get_settings()

    async with session_factory() as session:
        bootstrap_stmt = select(ChatSession).where(ChatSession.id == chat_session_id)
        chat_session = (await session.execute(bootstrap_stmt)).scalar_one_or_none()
        if chat_session is None:
            logger.warning(
                "send_whatsapp_reply: chat session %s not found; skipping", chat_session_id
            )
            return

        organization = await OrganizationRepository(session).get_or_404(
            chat_session.organization_id
        )

        if _outside_customer_window(
            chat_session, window_hours=settings.whatsapp_customer_window_hours
        ):
            _record_deferred_reply(chat_session, content=reply_content, reason=_DEFERRED_REASON)
            await session.commit()
            return

        wa_id = (chat_session.channel_metadata or {}).get("wa_id")

        try:
            await whatsapp_client.send_text(
                phone_number_id=organization.whatsapp_phone_number_id,
                to=wa_id,
                body=reply_content,
                access_token_encrypted=organization.whatsapp_access_token_encrypted,
            )
        except WhatsAppSendError as exc:
            if exc.meta_error_code == _META_OUTSIDE_WINDOW_ERROR_CODE:
                _record_deferred_reply(
                    chat_session, content=reply_content, reason=_DEFERRED_REASON
                )
                await session.commit()
                return
            if _is_transient_error(exc):
                job_try = ctx.get("job_try", 1)
                raise Retry(defer=2**job_try) from exc
            logger.warning(
                "permanent WhatsApp send failure chat_session_id=%s organization_id=%s "
                "http_status=%s meta_error_code=%s",
                chat_session_id,
                chat_session.organization_id,
                exc.status_code,
                exc.meta_error_code,
                exc_info=True,
            )
            return
        except Exception as exc:
            if _is_transient_error(exc):
                job_try = ctx.get("job_try", 1)
                raise Retry(defer=2**job_try) from exc
            logger.warning(
                "permanent WhatsApp send failure chat_session_id=%s organization_id=%s",
                chat_session_id,
                chat_session.organization_id,
                exc_info=True,
            )
            return

        logger.info(
            "WhatsApp reply sent chat_session_id=%s organization_id=%s",
            chat_session_id,
            chat_session.organization_id,
        )
