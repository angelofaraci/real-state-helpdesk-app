"""SLA monitor cron job (stage 6 — queues + SLA, PR6).

`monitor_sla` runs every 5 minutes (see
`app.workers.settings.WorkerSettings.cron_jobs`) and scans EVERY
non-terminal ticket with an `sla_due_at` set — across ALL organizations.

Like `app.workers.classification.sweep_pending_classifications`, this is
deliberately a cross-organization, direct `Ticket` query rather than one
run through the org-scoped `TicketRepository`: the monitor has no single
`OrgScope` to run under (it is a background system job spanning every
tenant), so scoping the READ would require enumerating every organization
and running one query per org for no benefit. Every WRITE this job makes
(`record_once`/`fan_out`), however, DOES run under a concrete
`OrgScope.for_background_worker(ticket.organization_id)` — the read is
cross-org, the writes are not.

Each candidate ticket is processed inside its own SAVEPOINT
(`session.begin_nested()`): if warning/breach recording or notification
fan-out raises for one ticket, that ticket's partial work rolls back, the
exception is logged, and the cron continues to the next ticket — one
ticket's failure must never abort the whole pass. Idempotency across runs
relies entirely on `sla_event_service.record_once`'s partial unique index
(`ux_sla_events_ticket_event_once`): this job carries no "already
processed" state of its own. A ticket already past 100% elapsed with no
prior `warning` event gets BOTH `warning` and `breached` recorded in the
same pass — breach recording never requires a warning to have fired
first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.core.sla_defaults import SLA_WARNING_ELAPSED_FRACTION
from app.models.enums import NotificationKind, SlaEventType, TicketStatus
from app.models.ticket import Ticket
from app.services.notification_service import fan_out
from app.services.sla_event_service import record_once

logger = logging.getLogger(__name__)

# Non-terminal ticket statuses are monitored; `escalated` is deliberately
# NOT terminal (a human agent is actively working an escalated ticket, and
# its SLA clock keeps running) — only `resolved`/`closed` stop monitoring.
_TERMINAL_STATUSES = (TicketStatus.RESOLVED, TicketStatus.CLOSED)


async def monitor_sla(ctx: dict[str, Any]) -> None:
    """Scan every non-terminal ticket with `sla_due_at` set, across all
    organizations, and record/notify `warning`/`breached` SLA events as
    each ticket crosses the 80%-elapsed and 100%-elapsed thresholds.

    Opens its own session via `ctx["session_factory"]` (the same
    worker-owned-session pattern as
    `app.workers.classification.sweep_pending_classifications`) and
    commits once at the end of the pass — per-ticket work is isolated via
    SAVEPOINTs, not by delaying the outer commit.
    """
    session_factory = ctx["session_factory"]
    now = datetime.now(UTC)

    async with session_factory() as session:
        stmt = select(Ticket).where(
            Ticket.sla_due_at.isnot(None),
            Ticket.status.notin_(_TERMINAL_STATUSES),
            Ticket.created_at
            + (Ticket.sla_due_at - Ticket.created_at) * SLA_WARNING_ELAPSED_FRACTION
            <= now,
        )
        candidates = (await session.execute(stmt)).scalars().all()

        for ticket in candidates:
            try:
                async with session.begin_nested():
                    await _process_ticket(session, ticket, now=now)
            except Exception:
                logger.exception(
                    "SLA monitor failed processing ticket %s; continuing "
                    "with the rest of this pass",
                    ticket.id,
                )

        await session.commit()


async def _process_ticket(session: AsyncSession, ticket: Ticket, *, now: datetime) -> None:
    """Record/notify the `warning` and/or `breached` SLA event for a single
    candidate `ticket`, under its own org's `OrgScope`. Both checks are
    independent: a ticket already past `sla_due_at` with no prior
    `warning` event gets BOTH events recorded in this same call — breach
    recording never requires warning to have fired first.
    """
    scope = OrgScope.for_background_worker(ticket.organization_id)
    warn_at = ticket.created_at + (
        ticket.sla_due_at - ticket.created_at
    ) * SLA_WARNING_ELAPSED_FRACTION

    if now >= warn_at:
        warning_event_id = await record_once(
            session,
            scope=scope,
            ticket_id=ticket.id,
            event=SlaEventType.WARNING,
            sla_due_at=ticket.sla_due_at,
        )
        if warning_event_id is not None:
            await fan_out(
                session,
                scope=scope,
                ticket=ticket,
                sla_event_id=warning_event_id,
                kind=NotificationKind.SLA_WARNING,
            )

    if now >= ticket.sla_due_at:
        breach_event_id = await record_once(
            session,
            scope=scope,
            ticket_id=ticket.id,
            event=SlaEventType.BREACHED,
            sla_due_at=ticket.sla_due_at,
        )
        if breach_event_id is not None:
            await fan_out(
                session,
                scope=scope,
                ticket=ticket,
                sla_event_id=breach_event_id,
                kind=NotificationKind.SLA_BREACHED,
            )
