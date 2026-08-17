"""Message service: read (`list_messages`) and write (`create_message`)
access to a ticket's conversation thread (Work Unit 8).

Both functions resolve the parent ticket via `TicketRepository.get_or_404`
FIRST — which already fuses org scoping with role-based visibility (Rule
C, see `app.repositories.ticket_repository`'s docstring) — before ever
touching `Message` rows. A caller who cannot see the parent ticket (wrong
org, or a same-org-but-role-invisible ticket, e.g. one tenant's ticket
fetched by another tenant) gets `app.core.exceptions.NotFoundError` (404)
on both operations, through the exact same code path that already governs
direct ticket access. See `app.repositories.message_repository`'s module
docstring for why this pre-check — not `MessageRepository` alone — is the
real authorization boundary here.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.enums import AuthorType, UserRole
from app.models.message import Message
from app.repositories.message_repository import MessageRepository
from app.repositories.ticket_repository import TicketRepository

# Roles whose messages are recorded as `AuthorType.AGENT` — every other org
# role (tenant/owner) is recorded as `AuthorType.USER`. `AuthorType.BOT` is
# never derived here through a human-facing create; it is only ever set by
# `app.services.rag.suggest_response` (Stage 3, PR6) when persisting an AI
# suggestion.
_STAFF_ROLES = (UserRole.AGENT, UserRole.ADMIN)


class InvalidSuggestionReferenceError(ValueError):
    """Raised by `create_message` when `based_on_suggestion_id` does not
    resolve to an AI-suggestion message on the same ticket (or would be a
    self-reference). Mapped to 422 by the API layer."""


async def list_messages(
    session: AsyncSession, *, scope: OrgScope, ticket_id: UUID
) -> list[Message]:
    """List a ticket's messages, oldest first. Raises `NotFoundError` if
    the ticket does not exist, belongs to a different organization, or is
    not visible to the caller's role.

    Q1 visibility rule (design D6): AI-suggestion messages
    (`is_ai_suggestion=True`) are hidden from tenant/owner callers — they
    only see messages once an agent/admin has acted on (or replied
    referencing) a suggestion. Agent/admin callers see the full thread,
    including suggestions, since they are the ones who review and act on
    them. This filter is intentionally SERVICE-layer, not
    repository/query-layer: `app.services.rag`'s own internal queries
    (`_latest_suggestion`/`_is_fresh`) must keep seeing suggestions
    regardless of the caller's role — they are not reachable through this
    function."""
    await TicketRepository(session, scope).get_or_404(ticket_id)

    stmt = select(Message).where(Message.ticket_id == ticket_id)
    if scope.role not in _STAFF_ROLES:
        stmt = stmt.where(Message.is_ai_suggestion.is_(False))
    stmt = stmt.order_by(Message.created_at, Message.id)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _validate_suggestion_reference(
    session: AsyncSession, *, ticket_id: UUID, based_on_suggestion_id: UUID, new_message_id: UUID
) -> None:
    """Raise `InvalidSuggestionReferenceError` unless
    `based_on_suggestion_id` resolves to an existing AI-suggestion message
    on the same ticket. Also rejects the (structurally near-impossible,
    since the new message's id is server-generated) self-reference case —
    defense-in-depth mirroring the DB's own
    `ck_messages_based_on_suggestion_not_self` CHECK constraint, so a
    collision surfaces as a clean 422 instead of a raw 500 from the DB."""
    if based_on_suggestion_id == new_message_id:
        raise InvalidSuggestionReferenceError("a message cannot be based on itself")

    stmt = select(Message).where(Message.id == based_on_suggestion_id)
    referenced = (await session.execute(stmt)).scalar_one_or_none()

    if (
        referenced is None
        or referenced.ticket_id != ticket_id
        or not referenced.is_ai_suggestion
    ):
        raise InvalidSuggestionReferenceError(
            f"based_on_suggestion_id {based_on_suggestion_id} does not reference an "
            "AI-suggestion message on this ticket"
        )


async def create_message(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket_id: UUID,
    content: str,
    based_on_suggestion_id: UUID | None = None,
) -> Message:
    """Post a new message to a ticket's thread. Raises `NotFoundError`
    under the same visibility rule as `list_messages`.

    `author_type` is derived from `scope.role`, never accepted from the
    caller: `AGENT`/`ADMIN` -> `AuthorType.AGENT`, `TENANT`/`OWNER` ->
    `AuthorType.USER`. `is_ai_suggestion` always defaults to `False` here
    (a human-authored reply, never an AI suggestion — see
    `app.services.rag.suggest_response` for how those are created).

    `based_on_suggestion_id`, when provided, links this reply to the AI
    suggestion it was drafted from (Q1's mechanism). Raises
    `InvalidSuggestionReferenceError` (mapped to 422) if it does not
    resolve to an existing `is_ai_suggestion=True` message on this same
    ticket, or would self-reference."""
    await TicketRepository(session, scope).get_or_404(ticket_id)

    author_type = AuthorType.AGENT if scope.role in _STAFF_ROLES else AuthorType.USER

    new_id = uuid.uuid4()
    if based_on_suggestion_id is not None:
        await _validate_suggestion_reference(
            session,
            ticket_id=ticket_id,
            based_on_suggestion_id=based_on_suggestion_id,
            new_message_id=new_id,
        )

    repo = MessageRepository(session, scope)
    message = repo.add(
        id=new_id,
        ticket_id=ticket_id,
        author_type=author_type,
        content=content,
        is_ai_suggestion=False,
        based_on_suggestion_id=based_on_suggestion_id,
    )
    await session.flush()
    return message
