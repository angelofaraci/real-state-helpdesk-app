"""Org-scoped ticket read endpoints (Work Unit 7a).

Read access (`GET`) is available to ALL org roles (tenant/owner/agent/admin)
via `require_org_member` — unlike properties/contracts/categories, ticket
browsing is not a staff-only concept: every role can see tickets, just
different subsets. The subset narrowing happens inside
`app.repositories.ticket_repository.TicketRepository.select()`, not at this
dependency layer, so a cross-org ticket and a same-org-but-role-invisible
ticket (e.g. one tenant fetching another tenant's ticket) both surface as
404 through the exact same code path.

Ticket creation (`POST`) and status/agent-assignment updates (`PATCH`) are
Work Unit 7b and are deliberately NOT implemented here.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_member
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.schemas.ticket import TicketResponse
from app.services import ticket_service

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketResponse])
async def list_tickets(
    status: TicketStatus | None = None,
    category_id: UUID | None = None,
    urgency_id: UUID | None = None,
    agent_id: UUID | None = None,
    property_id: UUID | None = None,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> list[Ticket]:
    """List tickets visible to the caller (org + role), optionally filtered
    by status/category/urgency/agent/property."""
    return await ticket_service.list_tickets(
        session,
        scope=scope,
        status=status,
        category_id=category_id,
        urgency_id=urgency_id,
        agent_id=agent_id,
        property_id=property_id,
    )


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: UUID,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Ticket:
    """Fetch a single ticket visible to the caller (org + role). A
    cross-org or role-invisible id is indistinguishable from a missing one
    and surfaces as 404."""
    return await ticket_service.get_ticket(session, scope=scope, ticket_id=ticket_id)
