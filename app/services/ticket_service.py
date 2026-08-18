"""Ticket service: read access (`list_tickets`/`get_ticket`, Work Unit 7a)
plus creation and status/agent-assignment updates (Work Unit 7b).

The read functions delegate to `TicketRepository`, which fuses org scoping
with role-based visibility into `select()` itself (see that module's
docstring for Rule C) — so a cross-org ticket and a same-org-but-role
-invisible ticket both surface as `app.core.exceptions.NotFoundError` (404)
through the exact same code path, with no extra checks needed here.

`create_ticket` implements Rule A's exact 8-step validation order; see its
own docstring. `update_ticket` implements Rule B (manual-only agent
assignment) plus the tenant/owner status restriction; see its own
docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import (
    ClassificationStatus,
    ContractStatus,
    TicketChannel,
    TicketStatus,
    UserRole,
)
from app.models.ticket import Ticket
from app.repositories.category_repository import CategoryRepository
from app.repositories.classification_repository import ClassificationRepository
from app.repositories.contract_repository import ContractRepository
from app.repositories.property_repository import PropertyRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.urgency_level_repository import UrgencyLevelRepository
from app.repositories.user_repository import UserRepository

# Roles allowed to act as staff for ticket write operations: manual agent
# assignment (Rule B) and resolving/closing a ticket.
_STAFF_ROLES = (UserRole.AGENT, UserRole.ADMIN)


class TicketServiceError(Exception):
    """Base class for ticket write-operation business-rule violations
    (mapped to 422 by the API layer, except `ForbiddenError` -> 403)."""


class ContractPropertyMismatchError(TicketServiceError):
    """The referenced contract does not belong to the referenced property
    (Rule A, step 3)."""


class ContractNotActiveError(TicketServiceError):
    """The referenced contract's `status` is not `ContractStatus.ACTIVE`
    (Rule A, step 4)."""


class CategoryInactiveError(TicketServiceError):
    """The referenced category is not `active` (Rule A, step 5)."""


class UrgencyInactiveError(TicketServiceError):
    """The referenced urgency level is not `active` (Rule A, step 6)."""


class InvalidAgentError(TicketServiceError):
    """The referenced `agent_id` resolves to a same-org user, but their
    role is not `AGENT`/`ADMIN` (Rule B)."""


class ForbiddenError(TicketServiceError):
    """The acting role can never perform this action at all — a role gate
    checked before any resource lookup, mapped to 403 (Rule B)."""


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
    different organization, or is not visible to the caller's role.

    Also attaches the ticket's classification metadata (predicted
    category/urgency, confidence, `model_used`) from its `classifications`
    row, if one exists, as plain (non-mapped) attributes on the returned
    instance — `None` for each if the ticket has never been classified.
    This is stage 2 (PR5) plumbing for `GET /tickets/:id`; role-gating
    these fields (agent/admin only) happens at the API router, not here,
    since this function has no opinion on response shape."""
    ticket = await TicketRepository(session, scope).get_or_404(ticket_id)
    classification = await ClassificationRepository(session, scope).get_by_ticket_id(ticket_id)
    ticket.predicted_category = classification.predicted_category if classification else None
    ticket.confidence = classification.confidence if classification else None
    ticket.predicted_urgency = classification.predicted_urgency if classification else None
    ticket.urgency_confidence = classification.urgency_confidence if classification else None
    ticket.model_used = classification.model_used if classification else None
    return ticket


async def create_ticket(
    session: AsyncSession,
    *,
    scope: OrgScope,
    property_id: UUID | None,
    contract_id: UUID | None,
    title: str,
    description: str | None = None,
    category_id: UUID | None = None,
    urgency_id: UUID | None = None,
    channel: TicketChannel,
) -> Ticket:
    """Create a ticket in `scope`'s organization, enforcing Rule A's exact
    8-step validation order (CONFIRMED design — each step short-circuits
    the ones after it):

    1. `property_id` must resolve in scope (404 otherwise — a soft-deleted
       property is already excluded by `PropertyRepository.select()`'s
       default, so this covers "doesn't exist" and "soft-deleted"
       identically).
    2. `contract_id` must resolve in scope (404 otherwise).
    3. the contract must actually belong to `property_id`, not just exist
       somewhere in the org (422 `ContractPropertyMismatchError`).
    4. the contract must be `ContractStatus.ACTIVE` (422
       `ContractNotActiveError`).
    5. `category_id` must resolve in scope (404) and be `active` (422
       `CategoryInactiveError`).
    6. `urgency_id` must resolve in scope (404) and be `active` (422
       `UrgencyInactiveError`).
    7. if the caller is a `TENANT`, they must be the contract's tenant —
       otherwise 404 (not 403/422): a tenant creating a ticket against a
       contract that isn't theirs must not learn the contract exists at
       all.
    8. `sla_due_at` is computed once, from `urgency.sla_hours`, and frozen
       at creation time — later edits to `urgency.sla_hours` never
       retroactively change an already-created ticket's `sla_due_at`,
       since it is a stored timestamp, not a computed column.

    Steps 5, 6, and 8 (category/urgency lookup + SLA computation) run ONLY
    when both `category_id` and `urgency_id` are supplied — as of stage 2,
    `TicketCreate`'s validator guarantees they are both-or-neither, so a
    caller may omit both to defer classification to the async worker. When
    omitted, the ticket is created with `category_id=None`,
    `urgency_id=None`, `sla_due_at=None`, and
    `classification_status=ClassificationStatus.PENDING` (explicit, rather
    than relying on the column's DB-side default, so the in-memory instance
    reflects it immediately without a round trip).

    `property_id`/`contract_id` are ALSO optional (stage 4 — chatbot):
    when either is `None`, its resolution + the checks that depend on it
    (steps 1/2 for the missing one, step 3's mismatch check, step 4's
    active check, and step 7's tenant-ownership check — all of which
    require a resolved `contract`) are skipped entirely. This supports
    chat-originated tickets (e.g. `app.services.chat_tools.escalate_to_
    human`) that have no property/contract context at all. The
    authenticated/property-bearing flow (both ids supplied) is completely
    unaffected — every original check still runs in the same order.

    The reporter (`user_id`) is always `scope.user_id`; `status` starts at
    `OPEN`; `agent_id` starts unset; `organization_id` is injected by
    `TicketRepository.add()` (inherited from `ScopedRepository.add()`).
    """
    property_ = None
    if property_id is not None:
        property_ = await PropertyRepository(session, scope).get_or_404(property_id)

    contract = None
    if contract_id is not None:
        contract = await ContractRepository(session, scope).get_or_404(contract_id)

    if property_ is not None and contract is not None and contract.property_id != property_.id:
        raise ContractPropertyMismatchError(
            f"contract {contract_id} does not belong to property {property_id}"
        )
    if contract is not None and contract.status != ContractStatus.ACTIVE:
        raise ContractNotActiveError(f"contract {contract_id} is not active")

    urgency = None
    if category_id is not None and urgency_id is not None:
        category = await CategoryRepository(session, scope).get_or_404(category_id)
        if not category.active:
            raise CategoryInactiveError(f"category {category_id} is inactive")

        urgency = await UrgencyLevelRepository(session, scope).get_or_404(urgency_id)
        if not urgency.active:
            raise UrgencyInactiveError(f"urgency level {urgency_id} is inactive")

    if (
        contract is not None
        and scope.role == UserRole.TENANT
        and contract.tenant_id != scope.user_id
    ):
        raise NotFoundError("Contract", contract_id)

    fields: dict[str, object] = dict(
        user_id=scope.user_id,
        property_id=property_id,
        contract_id=contract_id,
        title=title,
        description=description,
        category_id=category_id,
        urgency_id=urgency_id,
        channel=channel,
        status=TicketStatus.OPEN,
        agent_id=None,
        closed_at=None,
    )
    if urgency is not None:
        fields["sla_due_at"] = datetime.now(UTC) + timedelta(hours=urgency.sla_hours)
    else:
        fields["sla_due_at"] = None
        fields["classification_status"] = ClassificationStatus.PENDING

    repo = TicketRepository(session, scope)
    ticket = repo.add(**fields)
    await session.flush()
    return ticket


async def update_ticket(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket_id: UUID,
    status: TicketStatus | None = None,
    agent_id: UUID | None = None,
    category_id: UUID | None = None,
    urgency_id: UUID | None = None,
) -> Ticket:
    """Update a ticket's `status`, `agent_id`, and/or `category_id`/
    `urgency_id` (stage 2 — manual recategorization).

    Role gates run BEFORE any resource lookup (per the app's status-code
    precedence, 403 outranks 404 — "can never do this action at all" is
    checked before the resource is even fetched):

    - assigning `agent_id` requires the caller's role to be `AGENT`/`ADMIN`
      (403 `ForbiddenError` otherwise, Rule B: manual-only assignment).
    - setting `status` to `RESOLVED`/`CLOSED` requires the caller's role to
      be `AGENT`/`ADMIN` (403 `ForbiddenError` otherwise) — tenant/owner
      can never resolve/close a ticket. Any other status value is settable
      by any role that can see the ticket (free-form transitions, no
      strict state machine, CONFIRMED design).
    - setting `category_id` and/or `urgency_id` requires the caller's role
      to be `AGENT`/`ADMIN` (403 `ForbiddenError` otherwise) — only staff
      may correct an AI classification (or classify manually, bypassing
      the async worker).

    After the role gates, the ticket must resolve in `scope` (404
    otherwise, standard `get_or_404` visibility). If `agent_id` is
    provided, it must resolve to a same-org user (404 if it doesn't) whose
    role is `AGENT`/`ADMIN` (422 `InvalidAgentError` otherwise).

    Setting `status=CLOSED` sets `closed_at = now()`; setting any other
    status clears `closed_at = None` — matching the DB CHECK constraint
    `ck_tickets_closed_at_matches_status`.

    Setting `category_id` and/or `urgency_id` (either one, not necessarily
    both) flags the ticket's existing `classifications` row as
    human-corrected (`ClassificationRepository.mark_human_corrected` — a
    no-op UPDATE if no such row exists yet, e.g. a ticket that was created
    with explicit taxonomy and never touched by the worker) and recomputes
    `sla_due_at` from the EFFECTIVE urgency level (the newly supplied
    `urgency_id`, or the ticket's existing `urgency_id` if only
    `category_id` was supplied) anchored on `ticket.created_at` — matching
    the async worker's SLA-anchor convention (`created_at + sla_hours`),
    NOT `datetime.now()`.

    Stage 3 (PR5): stashes a plain non-mapped `entered_resolved_or_closed`
    boolean on the returned instance (same "extra attribute on the ORM
    object" pattern `get_ticket` already uses for `predicted_category`
    etc.) — `True` only when THIS call's `status` moved the ticket from a
    non-terminal status into `RESOLVED`/`CLOSED` (old status captured
    before `repo.update()` mutates the object in place, compared against
    the new one), so the API route layer knows whether to enqueue
    `embed_resolved_ticket` without re-firing on an unrelated PATCH to an
    already-resolved/closed ticket or on re-sending the same status.
    """
    if agent_id is not None and scope.role not in _STAFF_ROLES:
        raise ForbiddenError("only agents/admins may assign a ticket to an agent")

    if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and scope.role not in _STAFF_ROLES:
        raise ForbiddenError("only agents/admins may resolve or close a ticket")

    if (category_id is not None or urgency_id is not None) and scope.role not in _STAFF_ROLES:
        raise ForbiddenError("only agents/admins may recategorize a ticket")

    repo = TicketRepository(session, scope)
    ticket = await repo.get_or_404(ticket_id)
    previous_status = ticket.status

    fields: dict[str, object] = {}

    if agent_id is not None:
        agent = await UserRepository(session, scope).get_or_404(agent_id)
        if agent.role not in _STAFF_ROLES:
            raise InvalidAgentError(
                "agent_id must reference a user with role 'agent' or 'admin'"
            )
        fields["agent_id"] = agent_id

    if status is not None:
        fields["status"] = status
        fields["closed_at"] = datetime.now(UTC) if status == TicketStatus.CLOSED else None

    if category_id is not None or urgency_id is not None:
        effective_urgency_id = urgency_id if urgency_id is not None else ticket.urgency_id
        urgency = await UrgencyLevelRepository(session, scope).get_or_404(effective_urgency_id)

        fields["category_id"] = category_id if category_id is not None else ticket.category_id
        fields["urgency_id"] = effective_urgency_id
        fields["sla_due_at"] = ticket.created_at + timedelta(hours=urgency.sla_hours)

        await ClassificationRepository(session, scope).mark_human_corrected(ticket_id)

    updated = await repo.update(ticket_id, **fields)

    # Stage 3 (PR5): non-mapped attribute, same pattern `get_ticket` uses
    # for `predicted_category` etc. — signals the API route layer whether
    # THIS patch is the transition edge into a terminal status, so it knows
    # whether to enqueue `embed_resolved_ticket` (see this function's
    # docstring).
    updated.entered_resolved_or_closed = (
        status is not None
        and status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
        and previous_status != status
    )

    return updated
