"""Chat orchestration (stage 4 — chatbot, PR4 — Orchestration).

`send_message` is the main entry point PR5's API route (`app/api/v1/chat.py`)
calls for every inbound visitor turn. Same house style as
`app.services.rag`/`app.services.ticket_service` — a plain `session` + `scope`
kwarg-only service function, not a worker job.

Retrieval reuses ONLY `KnowledgeChunkRepository.search` (never
`TicketEmbeddingRepository`) — a chat reply is grounded in knowledge-base
context, not past-ticket summaries; that distinction is deliberate (see the
module docstring of `app.services.rag`, whose `suggest_response` this module
does NOT call directly, since its "no source -> return `None`, never call the
LLM" rule conflicts with a chatbot's need to always produce a conversational
reply even with no grounded context).

Tool-dispatch loop: up to `settings.chat_max_tool_rounds` rounds are issued
to the LLM with `tools=` present; if the model still wants to call a tool
after the cap, one final call is issued with `tools` entirely ABSENT from
the kwargs (not an empty list) to force a plain text answer — any
`tool_calls` on that final response are ignored.

Low-confidence streak: a turn is "low confidence" when knowledge-base
retrieval returns zero chunks at/above `settings.rag_similarity_threshold`.
Two CONSECUTIVE low-confidence turns (not merely two low-confidence turns
ever) trigger the same automatic escalation `chat_tools.escalate_to_human`
performs — this module never reimplements that branching, it just calls the
function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import ChatMessageRole
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from app.services import chat_tools
from app.services.chat_tools import ChatToolContext

_SYSTEM_PROMPT = (
    "You are a helpful assistant for a property management helpdesk. Answer "
    "the visitor's questions using the provided knowledge base context when "
    "relevant. Use the available tools to look up ticket status, create "
    "tickets, schedule visits, or escalate to a human agent when needed. If "
    "you are not confident in an answer, say so honestly rather than "
    "guessing."
)

_AUTOMATIC_ESCALATION_REASON = "automatic: low confidence"


@dataclass(frozen=True)
class ChatTurnResult:
    """The result of a single `send_message` turn, returned to PR5's API
    route."""

    reply: str
    escalated: bool
    ticket_id: UUID | None
    tools_used: list[str] = field(default_factory=list)


def _to_openai_message(message: ChatMessage) -> dict[str, Any]:
    """Translate a persisted `ChatMessage` row into the OpenAI chat-
    completions message shape."""
    if message.role == ChatMessageRole.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    return {"role": message.role.value, "content": message.content}


def _tool_schemas_for(chat_session: ChatSession) -> list[dict[str, Any]]:
    """The subset of `chat_tools.TOOL_SCHEMAS` usable for `chat_session`,
    per `chat_tools.available_tool_names`."""
    available_names = set(chat_tools.available_tool_names(chat_session))
    return [
        schema
        for schema in chat_tools.TOOL_SCHEMAS
        if schema["function"]["name"] in available_names
    ]


async def _recent_history(
    session: AsyncSession, scope: OrgScope, chat_session_id: UUID, *, turns: int
) -> list[ChatMessage]:
    """The last `turns` messages for `chat_session_id`, oldest first."""
    stmt = ChatMessageRepository(session, scope).list_for_session(chat_session_id)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows[-turns:] if turns > 0 else []


async def send_message(
    session: AsyncSession,
    *,
    scope: OrgScope,
    chat_session: ChatSession,
    content: str,
    embedder: Any,
    llm_client: Any,
) -> ChatTurnResult:
    """Process one inbound visitor turn: persist it, retrieve grounded
    knowledge-base context, run the LLM tool-dispatch loop, persist the
    final assistant reply, and update the session's low-confidence streak
    (auto-escalating on two consecutive low-confidence turns)."""
    settings = get_settings()
    message_repo = ChatMessageRepository(session, scope)

    message_repo.add(
        chat_session_id=chat_session.id,
        role=ChatMessageRole.USER,
        content=content,
    )
    await session.flush()

    history = await _recent_history(
        session, scope, chat_session.id, turns=settings.chat_history_turns
    )

    embeddings = await embedder.embed([content])
    query_embedding = embeddings[0]
    chunk_rows = (
        await session.execute(
            KnowledgeChunkRepository(session, scope).search(
                query_embedding,
                min_similarity=settings.rag_similarity_threshold,
                limit=settings.rag_kb_top_k,
                overfetch=settings.rag_search_overfetch,
            )
        )
    ).all()
    low_confidence = len(chunk_rows) == 0

    system_content = _SYSTEM_PROMPT
    if chunk_rows:
        kb_context = "\n".join(f"- {chunk.content}" for chunk, _similarity in chunk_rows)
        system_content += f"\n\nRelevant knowledge base excerpts:\n{kb_context}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    messages.extend(_to_openai_message(message) for message in history)

    tool_schemas = _tool_schemas_for(chat_session)
    ctx = ChatToolContext(session=session, scope=scope, chat_session=chat_session)
    tools_used: list[str] = []

    reply_text = ""
    for _round in range(settings.chat_max_tool_rounds):
        completion = await llm_client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
        )
        choice_message = completion.choices[0].message
        tool_calls = getattr(choice_message, "tool_calls", None)
        if not tool_calls:
            reply_text = choice_message.content or ""
            break

        messages.append(
            {
                "role": "assistant",
                "content": choice_message.content or "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
        )

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments or "{}")
            handler = chat_tools._TOOL_HANDLERS[tool_name]
            tool_result = await handler(ctx, **tool_args)
            tools_used.append(tool_name)

            tool_result_json = json.dumps(tool_result)
            message_repo.add(
                chat_session_id=chat_session.id,
                role=ChatMessageRole.TOOL,
                content=tool_result_json,
                tool_name=tool_name,
                tool_call_id=tool_call.id,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result_json,
                }
            )
        await session.flush()
    else:
        # Hard cap reached and the model still wants to call a tool: force
        # a plain text answer by omitting `tools` entirely (not an empty
        # list) from the final call. Any `tool_calls` on this response are
        # ignored.
        completion = await llm_client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
        )
        reply_text = completion.choices[0].message.content or ""

    message_repo.add(
        chat_session_id=chat_session.id,
        role=ChatMessageRole.ASSISTANT,
        content=reply_text,
    )

    if low_confidence:
        chat_session.low_confidence_streak += 1
    else:
        chat_session.low_confidence_streak = 0

    escalated = False
    ticket_id = chat_session.ticket_id
    if chat_session.low_confidence_streak >= 2:
        escalation_result = await chat_tools.escalate_to_human(
            ctx, reason=_AUTOMATIC_ESCALATION_REASON
        )
        escalated = True
        result_ticket_id = escalation_result.get("ticket_id")
        ticket_id = UUID(result_ticket_id) if result_ticket_id else chat_session.ticket_id
        chat_session.low_confidence_streak = 0

    chat_session.last_activity_at = datetime.now(UTC)
    await session.flush()

    return ChatTurnResult(
        reply=reply_text,
        escalated=escalated,
        ticket_id=ticket_id,
        tools_used=tools_used,
    )
