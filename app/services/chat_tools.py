"""LLM tool-calling handlers for the stage-4 chatbot (PR3 — Business
Logic).

These are the functions the chat orchestration loop (PR4/PR5's
`app/api/v1/chat.py`) invokes when the LLM decides to call a tool. Every
handler takes a `ChatToolContext` bundling the request-scoped
`AsyncSession`, `OrgScope`, and the active `ChatSession` row, plus the
tool's own keyword arguments (as parsed from the LLM's tool-call JSON
arguments) — never a raw dict, so a handler's signature IS its contract.

Handlers never raise on "the caller did something the LLM should
gracefully explain back to the user" (a nonexistent ticket, an ambiguous
contract, a missing linked ticket) — they return a plain dict the LLM can
turn into a natural-language reply. They DO let unexpected/domain errors
(e.g. a DB failure) propagate, same as any other service function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.category import Category
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.contract import Contract
from app.models.enums import (
    AuthorType,
    ChatMessageRole,
    ChatSessionStatus,
    ContractStatus,
    TicketChannel,
    TicketStatus,
)
from app.repositories.category_repository import CategoryRepository
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.contract_repository import ContractRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.ticket_repository import TicketRepository
from app.services import ticket_service

# The category name chat-originated escalation tickets are filed under, if
# the org's taxonomy happens to have one — matched case-insensitively,
# never assumed to exist (an org's default taxonomy, see
# `app.core.taxonomy_defaults.DEFAULT_CATEGORIES`, has no "General" entry).
_GENERAL_CATEGORY_NAME = "general"

_CHAT_ORIGIN_NOTE = "This ticket was created from a chatbot conversation."


@dataclass(slots=True)
class ChatToolContext:
    """Everything a chat tool handler needs to act: the request-scoped
    `session`/`scope`, and the already-loaded active `chat_session` row."""

    session: AsyncSession
    scope: OrgScope
    chat_session: ChatSession

    @property
    def is_anonymous(self) -> bool:
        """`True` for a public widget visitor with no authenticated
        principal (`chat_session.user_id is None`, per `ChatSession`'s own
        anonymity rule — see that model's docstring)."""
        return self.chat_session.user_id is None


async def check_ticket_status(ctx: ChatToolContext, *, ticket_id: UUID) -> dict[str, Any]:
    """Look up a ticket's current status.

    `TicketRepository.get_or_404` already fuses org scoping with role-based
    visibility (Rule C), so a ticket that does not exist at all and a
    ticket that belongs to a different user both raise the exact same
    `NotFoundError` — this handler collapses both into the identical
    `{"found": False}` reply, with no signal distinguishing the two cases.
    """
    try:
        ticket = await TicketRepository(ctx.session, ctx.scope).get_or_404(ticket_id)
    except NotFoundError:
        return {"found": False}
    return {
        "found": True,
        "status": ticket.status.value,
        "category_id": str(ticket.category_id) if ticket.category_id else None,
        "created_at": ticket.created_at.isoformat(),
    }


async def _active_contracts_for_caller(ctx: ChatToolContext) -> list[Contract]:
    """The caller's ACTIVE contracts, in scope. Used by `create_ticket` to
    resolve which property/contract a chat-originated ticket is about."""
    stmt = (
        ContractRepository(ctx.session, ctx.scope)
        .select()
        .where(
            Contract.tenant_id == ctx.scope.user_id,
            Contract.status == ContractStatus.ACTIVE,
        )
    )
    result = await ctx.session.execute(stmt)
    return list(result.scalars().all())


async def _find_general_category_id(ctx: ChatToolContext) -> UUID | None:
    """The org's "General" category id, matched case-insensitively — or
    `None` if the org's taxonomy has no such category (never assumed to
    exist, see module docstring)."""
    stmt = CategoryRepository(ctx.session, ctx.scope).select().where(Category.active.is_(True))
    result = await ctx.session.execute(stmt)
    for category in result.scalars().all():
        if category.name.strip().lower() == _GENERAL_CATEGORY_NAME:
            return category.id
    return None


async def _copy_chat_messages_to_ticket(ctx: ChatToolContext, *, ticket_id: UUID) -> None:
    """Copy the session's user-authored chat turns onto the newly created
    ticket's message thread, plus one `AuthorType.BOT` note recording the
    chat origin. `role='tool'` chat messages (raw tool results, never
    user-facing) are skipped; only `role='user'` turns are copied — the
    ticket thread is meant for humans reading it later, not a full LLM
    transcript."""
    messages_stmt = ChatMessageRepository(ctx.session, ctx.scope).list_for_session(
        ctx.chat_session.id
    )
    result = await ctx.session.execute(messages_stmt)
    chat_messages: list[ChatMessage] = list(result.scalars().all())

    message_repo = MessageRepository(ctx.session, ctx.scope)
    for chat_message in chat_messages:
        if chat_message.role != ChatMessageRole.USER:
            continue
        message_repo.add(
            ticket_id=ticket_id,
            author_type=AuthorType.USER,
            content=chat_message.content,
            is_ai_suggestion=False,
        )
    message_repo.add(
        ticket_id=ticket_id,
        author_type=AuthorType.BOT,
        content=_CHAT_ORIGIN_NOTE,
        is_ai_suggestion=False,
    )


async def create_ticket(ctx: ChatToolContext, *, title: str, description: str) -> dict[str, Any]:
    """Create a ticket on behalf of the chat caller.

    Requires exactly one ACTIVE contract for the caller — zero or multiple
    ACTIVE contracts is ambiguous (which property/contract is this ticket
    about?) and returns a clarification request instead of guessing, so
    the LLM can ask the visitor to pick one.

    Taxonomy is deliberately left unset here (`category_id=None`,
    `urgency_id=None`), same as any other ticket created without upfront
    classification — the async classification worker picks it up later
    (see `ticket_service.create_ticket`'s docstring).
    """
    contracts = await _active_contracts_for_caller(ctx)
    if len(contracts) != 1:
        return {
            "needs_clarification": True,
            "options": [
                {"contract_id": str(contract.id), "property_id": str(contract.property_id)}
                for contract in contracts
            ],
        }

    contract = contracts[0]
    ticket = await ticket_service.create_ticket(
        ctx.session,
        scope=ctx.scope,
        property_id=contract.property_id,
        contract_id=contract.id,
        title=title,
        description=description,
        category_id=None,
        urgency_id=None,
        channel=TicketChannel.WEB,
    )

    await _copy_chat_messages_to_ticket(ctx, ticket_id=ticket.id)
    ctx.chat_session.ticket_id = ticket.id
    await ctx.session.flush()

    return {"ticket_id": str(ticket.id), "status": ticket.status.value}


async def schedule_visit(
    ctx: ChatToolContext, *, ticket_id: UUID, preferred_date: str, notes: str | None = None
) -> dict[str, Any]:
    """Request a property visit for the session's linked ticket — a stub:
    there is no dedicated visit-scheduling table/model (CONFIRMED design),
    this simply appends a note to the ticket's message thread for a human
    agent to act on.

    Requires `ctx.chat_session.ticket_id` to already be set AND to match
    `ticket_id` — a chat session with no linked ticket (never escalated /
    no ticket created yet) cannot schedule a visit, and no ticket-adjacent
    write happens in that case.
    """
    if ctx.chat_session.ticket_id is None:
        return {
            "error": "no ticket is linked to this chat session yet; "
            "create or escalate a ticket first"
        }
    if ctx.chat_session.ticket_id != ticket_id:
        return {"error": "ticket_id does not match this chat session's linked ticket"}

    ticket = await TicketRepository(ctx.session, ctx.scope).get_or_404(ticket_id)

    content = f"Visitor requested a visit for {preferred_date}."
    if notes:
        content += f" Notes: {notes}"

    MessageRepository(ctx.session, ctx.scope).add(
        ticket_id=ticket.id,
        author_type=AuthorType.BOT,
        content=content,
        is_ai_suggestion=False,
    )
    await ctx.session.flush()

    return {"scheduled": True, "ticket_id": str(ticket.id), "preferred_date": preferred_date}


async def escalate_to_human(ctx: ChatToolContext, *, reason: str) -> dict[str, Any]:
    """Hand the conversation off to a human agent.

    Three cases (CONFIRMED design):

    - **Anonymous** (`ctx.is_anonymous`): no `users` row exists to own a
      ticket, so no ticket is ever created — only `chat_session.status`
      moves to `ESCALATED`; `ticket_id` stays `None`.
    - **Identified, no linked ticket**: a ticket is created first (via
      `ticket_service.create_ticket`, with no property/contract context —
      `property_id=None`/`contract_id=None`, per its stage-4 chat-origin
      path — and the org's "General" category if one exists, else
      `category_id=None`), then its `status` is moved to `ESCALATED`
      through a DIRECT `TicketRepository.update()` call — deliberately NOT
      `ticket_service.update_ticket`, whose role gates and status handling
      were designed around the RESOLVED/CLOSED terminal set, not this
      chat-originated `ESCALATED` transition.
    - **Identified, already has a linked ticket**: no second ticket is
      created — only the existing ticket's `status` (and the chat
      session's) move to `ESCALATED`.

    In every non-anonymous case, `chat_session.status` also moves to
    `ESCALATED` last.
    """
    if ctx.is_anonymous:
        ctx.chat_session.status = ChatSessionStatus.ESCALATED
        await ctx.session.flush()
        return {"escalated": True, "ticket_id": None}

    if ctx.chat_session.ticket_id is None:
        category_id = await _find_general_category_id(ctx)
        ticket = await ticket_service.create_ticket(
            ctx.session,
            scope=ctx.scope,
            property_id=None,
            contract_id=None,
            title="Chat escalation",
            description=reason,
            category_id=category_id,
            urgency_id=None,
            channel=TicketChannel.WEB,
        )
        ticket_id = ticket.id
        ctx.chat_session.ticket_id = ticket_id
    else:
        ticket_id = ctx.chat_session.ticket_id

    await TicketRepository(ctx.session, ctx.scope).update(ticket_id, status=TicketStatus.ESCALATED)
    ctx.chat_session.status = ChatSessionStatus.ESCALATED
    await ctx.session.flush()

    return {"escalated": True, "ticket_id": str(ticket_id)}


# ---------------------------------------------------------------------------
# OpenAI tool-calling schemas + registry — consumed by PR4/PR5's chat
# orchestration loop.
# ---------------------------------------------------------------------------

CHECK_TICKET_STATUS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "check_ticket_status",
        "description": "Look up the current status of a support ticket by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "The ticket's UUID.",
                },
            },
            "required": ["ticket_id"],
        },
    },
}

CREATE_TICKET_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_ticket",
        "description": (
            "Create a new support ticket for the visitor's issue, resolving "
            "their active contract automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short summary of the issue."},
                "description": {
                    "type": "string",
                    "description": "The full description of the issue.",
                },
            },
            "required": ["title", "description"],
        },
    },
}

SCHEDULE_VISIT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "schedule_visit",
        "description": "Request a property visit for the session's linked ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "The linked ticket's UUID.",
                },
                "preferred_date": {
                    "type": "string",
                    "description": "The visitor's preferred visit date/time, as free text.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional extra notes about the visit request.",
                },
            },
            "required": ["ticket_id", "preferred_date"],
        },
    },
}

ESCALATE_TO_HUMAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "escalate_to_human",
        "description": "Hand the conversation off to a human agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the conversation needs a human agent.",
                },
            },
            "required": ["reason"],
        },
    },
}

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    CHECK_TICKET_STATUS_SCHEMA,
    CREATE_TICKET_SCHEMA,
    SCHEDULE_VISIT_SCHEMA,
    ESCALATE_TO_HUMAN_SCHEMA,
)

_TOOL_HANDLERS: dict[str, Any] = {
    "check_ticket_status": check_ticket_status,
    "create_ticket": create_ticket,
    "schedule_visit": schedule_visit,
    "escalate_to_human": escalate_to_human,
}


def available_tool_names(chat_session: ChatSession) -> list[str]:
    """The tool names usable for `chat_session`. An anonymous session
    (`user_id is None`) has no ticket/contract visibility at all (see
    `OrgScope.for_anonymous_chat`'s docstring) and cannot own tickets, so
    only `escalate_to_human` is offered — every other tool is
    identified-user-only."""
    if chat_session.user_id is None:
        return ["escalate_to_human"]
    return list(_TOOL_HANDLERS.keys())
