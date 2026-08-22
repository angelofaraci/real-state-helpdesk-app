"""Notification recipient resolution + fan-out (stage 6, PR5), plus the
read-side `list_notifications`/`mark_read` backing the notifications API
(stage 6, PR7b).

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

`list_notifications`/`mark_read` (PR7b) both build on top of
`NotificationRepository.for_recipient`, never a raw scoped query: a
notification addressed to another user (same org) or belonging to another
org is structurally unreachable through that query, which is exactly what
gives `mark_read` its "404, not 403" cross-tenant behavior for free —
`get_notification_or_404` just runs that same query filtered to a single
id and raises `NotFoundError` when it comes back empty, mirroring
`ScopedRepository.get_or_404`'s contract one level up (scoped by recipient,
not just by org).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.scope import OrgScope
from app.models.enums import NotificationKind, UserRole, UserStatus
from app.models.notification import Notification
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


async def list_notifications(
    session: AsyncSession, *, scope: OrgScope, limit: int, offset: int
) -> tuple[list[Notification], int]:
    """Page through `scope.user_id`'s own notifications within `scope`'s
    organization, newest first (matches
    `NotificationRepository.for_recipient`'s ordering, which matches the
    `ix_notifications_user_id_sent_at_id` composite index).

    Returns `(items, total)`, where `total` is the full matching count
    ignoring `limit`/`offset` (so a caller can compute `has_more` /
    render a page count), computed via a single extra `COUNT(*)` query
    over the same `for_recipient` selectable — never a second, separately
    -maintained scoped query.
    """
    repo = NotificationRepository(session, scope)
    base = repo.for_recipient(scope.user_id)

    total_result = await session.execute(select(func.count()).select_from(base.subquery()))
    total = total_result.scalar_one()

    page_result = await session.execute(base.limit(limit).offset(offset))
    items = list(page_result.scalars().all())

    return items, total


async def get_notification_or_404(
    session: AsyncSession, *, scope: OrgScope, notification_id: UUID
) -> Notification:
    """Fetch a single notification addressed to `scope.user_id` within
    `scope`'s organization. Raises `NotFoundError` if it does not exist,
    belongs to a different organization, or is addressed to a different
    user — all three are indistinguishable from the caller's point of
    view, by construction of `for_recipient`."""
    repo = NotificationRepository(session, scope)
    stmt = repo.for_recipient(scope.user_id).where(Notification.id == notification_id)
    result = await session.execute(stmt)
    notification = result.scalar_one_or_none()
    if notification is None:
        raise NotFoundError("Notification", notification_id)
    return notification


async def mark_read(
    session: AsyncSession, *, scope: OrgScope, notification_id: UUID
) -> Notification:
    """Mark a notification addressed to `scope.user_id` as read.

    Idempotent: if `read_at` is already set, it is left untouched (never
    overwritten with a newer timestamp) and nothing is flushed — calling
    this twice in a row has the exact same observable effect as calling it
    once. Raises `NotFoundError` (-> 404, not 403) for a notification that
    does not belong to the caller, same as `get_notification_or_404`.
    """
    notification = await get_notification_or_404(
        session, scope=scope, notification_id=notification_id
    )
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.flush()
    return notification
