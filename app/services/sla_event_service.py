"""SLA event write path (stage 6, PR5).

`record_once`/`record_resolved` are the sole writers of `sla_events` rows.

`record_once` backs the (later PR's) SLA-monitor cron's warning/breach
emission: it enforces migration 0009's partial unique index
(`ux_sla_events_ticket_event_once`) at the application layer via
`INSERT ... ON CONFLICT ... DO NOTHING`, so a re-run of the cron (or a
concurrent one) never double-fires a warning/breach event for the same
ticket. It returns the new row's id on a fresh insert, or `None` if a row
already existed — the caller's contract is to treat `None` as "skip
notification fan-out", not an error.

`record_resolved` backs `ticket_service.update_ticket`'s synchronous
resolve/close hook. `resolved` is deliberately NOT covered by that unique
index (a ticket's reopen/re-resolve cycle may record it more than once),
so this is a plain insert with no conflict handling at all.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.models.enums import SlaEventType
from app.models.sla_event import SlaEvent
from app.repositories.sla_event_repository import SlaEventRepository


async def record_once(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket_id: UUID,
    event: SlaEventType,
    sla_due_at: datetime | None = None,
) -> UUID | None:
    """Insert a `warning`/`breached` `sla_events` row for `ticket_id`,
    unless one already exists for `(ticket_id, event)`
    (`ux_sla_events_ticket_event_once`).

    Returns the new row's id on a fresh insert, or `None` if a row already
    existed — the caller's signal to skip notification fan-out (an
    already-recorded event must never re-notify).

    `index_where` MUST match migration 0009's raw-SQL partial index
    predicate (`event IN ('warning', 'breached')`) exactly: Postgres
    infers the `ON CONFLICT` target from `index_elements` + `index_where`
    together, and a mismatched predicate means it cannot find a matching
    index at all — the INSERT raises instead of silently no-op'ing.
    """
    stmt = (
        pg_insert(SlaEvent)
        .values(
            organization_id=scope.organization_id,
            ticket_id=ticket_id,
            event=event,
            sla_due_at=sla_due_at,
        )
        .on_conflict_do_nothing(
            index_elements=["ticket_id", "event"],
            index_where=text("event IN ('warning', 'breached')"),
        )
        .returning(SlaEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def record_resolved(
    session: AsyncSession,
    *,
    scope: OrgScope,
    ticket_id: UUID,
    sla_due_at: datetime | None = None,
) -> UUID:
    """Insert a `resolved` `sla_events` row for `ticket_id`. Always
    inserts — `resolved` is deliberately excluded from
    `ux_sla_events_ticket_event_once`, so a ticket's reopen/re-resolve
    cycle may legitimately record it more than once. No notification is
    ever sent for a `resolved` event (only `warning`/`breached` notify)."""
    repo = SlaEventRepository(session, scope)
    instance = repo.add(ticket_id=ticket_id, event=SlaEventType.RESOLVED, sla_due_at=sla_due_at)
    await session.flush()
    return instance.id
