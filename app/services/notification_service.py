"""Notification recipient resolution + fan-out (stage 6, PR5).

RESOLVED decision: the SLA warning/breach recipient set — and the default-
queue recipient set a later PR resolves the same way — is every `ACTIVE`
`role=ADMIN` user in the ticket's organization, PLUS the ticket's assigned
agent if `agent_id` is set and that user is `ACTIVE` (any role). There is
no `users.supervisor_id` column and no queue table; this resolver is the
single source of truth for "who gets notified about this ticket".

Recipients are ALWAYS resolved fresh via a live query at send time — never
cached, never denormalized onto the ticket/event row. Zero active admins
combined with an unassigned ticket is a normal, valid outcome:
`resolve_recipients` returns `[]`, and `fan_out` treats that as "send
zero notifications", never raising.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.enums import NotificationKind, UserRole, UserStatus
from app.models.ticket import Ticket
from app.models.user import User
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository


async def resolve_recipients(
    session: AsyncSession, *, scope: OrgScope, ticket: Ticket
) -> list[User]:
    """Resolve the live set of users who should be notified about
    `ticket`: every `ACTIVE` `role=ADMIN` user in `scope`'s organization,
    plus `ticket.agent_id`'s user if assigned and `ACTIVE` (any role).
    Deduplicated by user id (the assigned agent may also be an admin).

    Returns `[]` when there are no active admins and no active assigned
    agent — a normal, valid outcome, never an error.
    """
    repo = UserRepository(session, scope)

    admins_result = await session.execute(
        repo.list(role=UserRole.ADMIN, status=UserStatus.ACTIVE)
    )
    recipients: dict[UUID, User] = {user.id: user for user in admins_result.scalars().all()}

    if ticket.agent_id is not None:
        agent_stmt = repo.select().where(
            User.id == ticket.agent_id, User.status == UserStatus.ACTIVE
        )
        agent_result = await session.execute(agent_stmt)
        agent = agent_result.scalar_one_or_none()
        if agent is not None:
            recipients[agent.id] = agent

    return list(recipients.values())


def _title_for(kind: NotificationKind, ticket: Ticket) -> str:
    if kind == NotificationKind.SLA_WARNING:
        return f"SLA warning: ticket {ticket.id}"
    return f"SLA breached: ticket {ticket.id}"


async def fan_out(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket: Ticket,
    sla_event_id: UUID,
    kind: NotificationKind,
) -> list[User]:
    """Create one `Notification` row per resolved recipient (see
    `resolve_recipients`) for a newly-recorded `warning`/`breached`
    `sla_events` row. Creates zero rows and does not raise when
    `resolve_recipients` returns `[]`.

    Returns the list of recipients notified (empty if none).
    """
    recipients = await resolve_recipients(session, scope=scope, ticket=ticket)
    if not recipients:
        return []

    repo = NotificationRepository(session, scope)
    title = _title_for(kind, ticket)
    for recipient in recipients:
        repo.add(
            user_id=recipient.id,
            ticket_id=ticket.id,
            sla_event_id=sla_event_id,
            kind=kind,
            title=title,
        )

    return recipients
