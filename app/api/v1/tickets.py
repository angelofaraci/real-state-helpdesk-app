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

import openai
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_member, require_org_staff
from app.api.deps_rag import get_llm_client, get_rag_embedder
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.enums import ClassificationStatus, TicketStatus, UserRole
from app.models.message import Message
from app.models.ticket import Ticket
from app.schemas.message import MessageCreate, MessageResponse
from app.schemas.rag import SuggestedResponseOut, SuggestionOut
from app.schemas.ticket import TicketCreate, TicketPatch, TicketResponse
from app.services import message_service, ticket_service
from app.services import rag as rag_service
from app.services.message_service import InvalidSuggestionReferenceError
from app.services.rag_embeddings import RagEmbeddingProvider
from app.services.ticket_service import (
    CategoryInactiveError,
    ContractNotActiveError,
    ContractPropertyMismatchError,
    ForbiddenError,
    InvalidAgentError,
    UrgencyInactiveError,
)
from app.workers.classification import enqueue_classification
from app.workers.email import enqueue_ticket_email_reply
from app.workers.rag import enqueue_resolved_ticket_embedding

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])

# Roles that may see a ticket's classification metadata (predicted
# category/urgency, confidence, model_used) on `GET /tickets/:id` — see
# `get_ticket` below. Mirrors `ticket_service._STAFF_ROLES`.
_STAFF_ROLES = (UserRole.AGENT, UserRole.ADMIN)


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
) -> TicketResponse:
    """Fetch a single ticket visible to the caller (org + role). A
    cross-org or role-invisible id is indistinguishable from a missing one
    and surfaces as 404.

    Classification metadata (predicted category/urgency, confidence,
    model_used — see `ticket_service.get_ticket`) is visible only to
    agent/admin roles; a tenant/owner sees these fields as `null`, matching
    the fact that they never see the raw AI prediction, only the resolved
    `category_id`/`urgency_id` once classified."""
    ticket = await ticket_service.get_ticket(session, scope=scope, ticket_id=ticket_id)
    response = TicketResponse.model_validate(ticket)
    if scope.role not in _STAFF_ROLES:
        response.predicted_category = None
        response.confidence = None
        response.predicted_urgency = None
        response.urgency_confidence = None
        response.model_used = None
    return response


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    background_tasks: BackgroundTasks,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Ticket:
    """Create a ticket in the caller's organization. See
    `app.services.ticket_service.create_ticket`'s docstring for Rule A's
    exact 8-step validation order; a missing/cross-org property, contract,
    category, or urgency (as well as a tenant referencing a contract that
    isn't theirs) surfaces as 404, and a business-rule violation on an
    otherwise-visible resource surfaces as 422.

    When `category_id`/`urgency_id` are omitted, the created ticket ends up
    `classification_status="pending"`; a `classify_ticket` job is then
    enqueued via `BackgroundTasks` (runs after the response is sent) so the
    async worker picks it up. Supplying both upfront skips this entirely —
    nothing is enqueued."""
    ticket = await _create_ticket_or_422(session, scope, payload)

    if ticket.classification_status == ClassificationStatus.PENDING:
        background_tasks.add_task(enqueue_classification, ticket.id, ticket.organization_id)

    return ticket


async def _create_ticket_or_422(
    session: AsyncSession, scope: OrgScope, payload: TicketCreate
) -> Ticket:
    try:
        return await ticket_service.create_ticket(
            session,
            scope=scope,
            property_id=payload.property_id,
            contract_id=payload.contract_id,
            title=payload.title,
            description=payload.description,
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
    background_tasks: BackgroundTasks,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Ticket:
    """Update a ticket's `status` and/or `agent_id`. See
    `app.services.ticket_service.update_ticket`'s docstring for the role
    gates (Rule B): assigning an agent or resolving/closing a ticket
    requires an `AGENT`/`ADMIN` role (403 otherwise); an invalid
    `agent_id` surfaces as 404 (unresolvable) or 422 (wrong role).

    Stage 3 (PR5): when this PATCH is the actual transition edge into
    `RESOLVED`/`CLOSED` (`ticket_service.update_ticket`'s
    `entered_resolved_or_closed` flag on the returned ticket — `getattr`
    defaults to `False` since most other call sites/tests mock
    `update_ticket` without setting it), an `embed_resolved_ticket` job is
    enqueued via `BackgroundTasks` (runs after the response is sent),
    mirroring `create_ticket`'s `enqueue_classification` hook above. An
    unrelated PATCH to an already-resolved/closed ticket (e.g. just
    `agent_id`) or re-sending the same status enqueues nothing."""
    fields = payload.model_dump(exclude_unset=True)
    try:
        ticket = await ticket_service.update_ticket(
            session, scope=scope, ticket_id=ticket_id, **fields
        )
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvalidAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if getattr(ticket, "entered_resolved_or_closed", False):
        background_tasks.add_task(
            enqueue_resolved_ticket_embedding, ticket.id, ticket.organization_id
        )

    return ticket


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
    background_tasks: BackgroundTasks,
    scope: OrgScope = Depends(require_org_member),
    session: AsyncSession = Depends(get_session),
) -> Message:
    """Post a new message to a ticket's thread. `author_type` is derived
    server-side from the caller's role (see
    `app.services.message_service.create_message`) — it is not, and
    cannot be, a client-settable field on `MessageCreate`. A cross-org or
    role-invisible `ticket_id` surfaces as 404.

    `based_on_suggestion_id`, when supplied, must reference an existing
    AI-suggestion message on this same ticket (and cannot self-reference);
    otherwise 422 (`InvalidSuggestionReferenceError`).

    Stage 5 (PR3 — email outbound): when the created message's
    `outbound_email_required` flag (see `message_service.create_message`'s
    docstring) is truthy — a staff reply on a `channel=email` ticket — a
    `send_ticket_email_reply` job is enqueued via `BackgroundTasks` (runs
    after the response is sent), mirroring `create_ticket`'s
    `enqueue_classification` hook above. `getattr` defaults to `False`
    since most other call sites/tests mock `create_message` without setting
    it."""
    try:
        message = await message_service.create_message(
            session,
            scope=scope,
            ticket_id=ticket_id,
            content=payload.content,
            based_on_suggestion_id=payload.based_on_suggestion_id,
        )
    except InvalidSuggestionReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if getattr(message, "outbound_email_required", False):
        background_tasks.add_task(
            enqueue_ticket_email_reply, message.id, message.ticket_id, scope.organization_id
        )

    return message


@router.get("/{ticket_id}/suggested-response", response_model=SuggestedResponseOut)
async def get_suggested_response(
    ticket_id: UUID,
    scope: OrgScope = Depends(require_org_staff),
    session: AsyncSession = Depends(get_session),
    embedder: RagEmbeddingProvider = Depends(get_rag_embedder),
    llm_client: openai.AsyncOpenAI = Depends(get_llm_client),
) -> SuggestedResponseOut:
    """Return a grounded AI-drafted reply suggestion for this ticket
    (agent/admin only — see `app.services.rag.suggest_response`'s
    docstring for caching/freshness and the Q4 empty-retrieval rule). A
    cross-org or role-invisible `ticket_id` surfaces as 404.

    When no grounded source was found (Q4), `suggestion` is `null` and
    `reason` explains why — this is a 200, not an error."""
    result = await rag_service.suggest_response(
        session, scope=scope, ticket_id=ticket_id, embedder=embedder, llm_client=llm_client
    )
    if result is None:
        return SuggestedResponseOut(suggestion=None, reason="no grounded suggestion available")

    return SuggestedResponseOut(
        suggestion=SuggestionOut(
            message_id=result.message.id,
            content=result.message.content,
            top_similarity=result.top_similarity,
        )
    )
