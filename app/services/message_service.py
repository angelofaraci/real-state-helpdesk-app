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
# never derived here in stage 1 (no AI yet); it is unreachable through any
# human-facing code path.
_STAFF_ROLES = (UserRole.AGENT, UserRole.ADMIN)


async def list_messages(
    session: AsyncSession, *, scope: OrgScope, ticket_id: UUID
) -> list[Message]:
    """List a ticket's messages, oldest first. Raises `NotFoundError` if
    the ticket does not exist, belongs to a different organization, or is
    not visible to the caller's role."""
    await TicketRepository(session, scope).get_or_404(ticket_id)

    stmt = (
        select(Message)
        .where(Message.ticket_id == ticket_id)
        .order_by(Message.created_at, Message.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_message(
    session: AsyncSession, *, scope: OrgScope, ticket_id: UUID, content: str
) -> Message:
    """Post a new message to a ticket's thread. Raises `NotFoundError`
    under the same visibility rule as `list_messages`.

    `author_type` is derived from `scope.role`, never accepted from the
    caller: `AGENT`/`ADMIN` -> `AuthorType.AGENT`, `TENANT`/`OWNER` ->
    `AuthorType.USER`. `is_ai_suggestion` always defaults to `False` in
    stage 1 (no AI yet)."""
    await TicketRepository(session, scope).get_or_404(ticket_id)

    author_type = AuthorType.AGENT if scope.role in _STAFF_ROLES else AuthorType.USER

    repo = MessageRepository(session, scope)
    message = repo.add(
        ticket_id=ticket_id,
        author_type=author_type,
        content=content,
        is_ai_suggestion=False,
    )
    await session.flush()
    return message
