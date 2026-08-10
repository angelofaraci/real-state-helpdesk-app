"""Ticket service: read-only access for Work Unit 7a
(`list_tickets`/`get_ticket`).

Both functions delegate to `TicketRepository`, which fuses org scoping with
role-based visibility into `select()` itself (see that module's docstring
for Rule C) — so a cross-org ticket and a same-org-but-role-invisible ticket
both surface as `app.core.exceptions.NotFoundError` (404) through the exact
same code path, with no extra checks needed here.

Ticket creation (`POST`) and status/agent-assignment updates (`PATCH`) are
Work Unit 7b, not this module yet.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository


async def list_tickets(
    session: AsyncSession,
    *,
    scope: OrgScope,
    status: TicketStatus | None = None,
    category_id: UUID | None = None,
    urgency_id: UUID | None = None,
    agent_id: UUID | None = None,
    property_id: UUID | None = None,
) -> list[Ticket]:
    """List tickets visible to `scope` (org + role), optionally filtered by
    status/category/urgency/agent/property."""
    repo = TicketRepository(session, scope)
    stmt = repo.list(
        status=status,
        category_id=category_id,
        urgency_id=urgency_id,
        agent_id=agent_id,
        property_id=property_id,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_ticket(session: AsyncSession, *, scope: OrgScope, ticket_id: UUID) -> Ticket:
    """Fetch a single ticket visible to `scope` (org + role). Raises
    `app.core.exceptions.NotFoundError` if it does not exist, belongs to a
    different organization, or is not visible to the caller's role."""
    return await TicketRepository(session, scope).get_or_404(ticket_id)
