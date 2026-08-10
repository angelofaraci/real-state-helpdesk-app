"""Org-scoped ticket endpoints (Work Units 7a + 7b).

Read access (`GET`) is available to ALL org roles (tenant/owner/agent/admin)
via `require_org_member` — unlike properties/contracts/categories, ticket
browsing is not a staff-only concept: every role can see tickets, just
different subsets. The subset narrowing happens inside
`app.repositories.ticket_repository.TicketRepository.select()`, not at this
dependency layer, so a cross-org ticket and a same-org-but-role-invisible
ticket (e.g. one tenant fetching another tenant's ticket) both surface as
404 through the exact same code path.

Creation (`POST`) is likewise open to any org member via `require_org_member`
— any role can report a ticket; `ticket_service.create_ticket` enforces
Rule A's business rules. Updates (`PATCH`) are also open to any org member
at this dependency layer; the role gates for manual agent assignment and
resolving/closing a ticket live inside `ticket_service.update_ticket`
(Rule B), since they need to distinguish assignment/close-only from other
free-form status transitions, which a blanket dependency can't express.

`app.core.exceptions.NotFoundError` is handled globally (see `app.main`),
so 404 mapping needs no explicit `except` here.

Work Unit 8 extends this router with `/{ticket_id}/messages`
(`GET`/`POST`), also behind `require_org_member`: a caller who cannot see
the parent ticket gets the exact same 404 as direct ticket access, via
`app.services.message_service`'s `TicketRepository.get_or_404` pre-check —
just reached through a nested route.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_member
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import TicketStatus
from app.models.message import Message
from app.models.ticket import Ticket
from app.schemas.message import MessageCreate, MessageResponse
from app.schemas.ticket import TicketCreate, TicketPatch, TicketResponse
from app.services import message_service, ticket_service
from app.services.ticket_service import (
    CategoryInactiveError,
    ContractNotActiveError,
    ContractPropertyMismatchError,
    ForbiddenError,
    InvalidAgentError,
    UrgencyInactiveError,
)

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


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Ticket:
    """Create a ticket in the caller's organization. See
    `app.services.ticket_service.create_ticket`'s docstring for Rule A's
    exact 8-step validation order; a missing/cross-org property, contract,
    category, or urgency (as well as a tenant referencing a contract that
    isn't theirs) surfaces as 404, and a business-rule violation on an
    otherwise-visible resource surfaces as 422."""
    try:
        return await ticket_service.create_ticket(
            session,
            scope=scope,
            property_id=payload.property_id,
            contract_id=payload.contract_id,
            category_id=payload.category_id,
            urgency_id=payload.urgency_id,
            channel=payload.channel,
        )
    except (
        ContractPropertyMismatchError,
        ContractNotActiveError,
        CategoryInactiveError,
        UrgencyInactiveError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.patch("/{ticket_id}", response_model=TicketResponse)
async def update_ticket(
    ticket_id: UUID,
    payload: TicketPatch,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Ticket:
    """Update a ticket's `status` and/or `agent_id`. See
    `app.services.ticket_service.update_ticket`'s docstring for the role
    gates (Rule B): assigning an agent or resolving/closing a ticket
    requires an `AGENT`/`ADMIN` role (403 otherwise); an invalid
    `agent_id` surfaces as 404 (unresolvable) or 422 (wrong role)."""
    fields = payload.model_dump(exclude_unset=True)
    try:
        return await ticket_service.update_ticket(
            session, scope=scope, ticket_id=ticket_id, **fields
        )
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvalidAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/{ticket_id}/messages", response_model=list[MessageResponse])
async def list_messages(
    ticket_id: UUID,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> list[Message]:
    """List a ticket's messages, oldest first. A cross-org or
    role-invisible `ticket_id` is indistinguishable from a missing one and
    surfaces as 404 (see `app.services.message_service.list_messages`)."""
    return await message_service.list_messages(session, scope=scope, ticket_id=ticket_id)


@router.post(
    "/{ticket_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_message(
    ticket_id: UUID,
    payload: MessageCreate,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Message:
    """Post a new message to a ticket's thread. `author_type` is derived
    server-side from the caller's role (see
    `app.services.message_service.create_message`) — it is not, and
    cannot be, a client-settable field on `MessageCreate`. A cross-org or
    role-invisible `ticket_id` surfaces as 404."""
    return await message_service.create_message(
        session, scope=scope, ticket_id=ticket_id, content=payload.content
    )
