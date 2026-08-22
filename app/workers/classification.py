"""Async arq job(s) for stage-2 ticket classification.

`classify_ticket` is the per-ticket job the API layer enqueues right after
ticket creation (a later work unit); `sweep_pending_classifications` is a
cron job that re-enqueues any ticket still `classification_status="pending"`
after 5 minutes (e.g. because the queue lost a job across a worker
crash/restart, or the original enqueue never happened).

Both jobs open and commit their own `AsyncSession` via
`ctx["session_factory"]` — deliberately NOT the request-scoped `get_session`
FastAPI dependency (`app.core.session`), since a worker job has no request
lifecycle to hang a dependency off of. `ctx["session_factory"]` is expected
to behave like a real `sqlalchemy.ext.asyncio.async_sessionmaker` instance:
calling it returns an async context manager yielding an `AsyncSession`. See
`app.workers.settings.WorkerSettings.on_startup` for how it (and
`ctx["embedder"]`) are actually built in production.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from arq import Retry, create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.enums import ClassificationStatus
from app.models.ticket import Ticket
from app.repositories.category_repository import CategoryRepository
from app.repositories.classification_repository import ClassificationRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.urgency_level_repository import UrgencyLevelRepository
from app.services.classifier import (
    ClassificationTransientError,
    ClassificationUnavailableError,
    TaxonomyOption,
)
from app.services.classifier import classify as default_classify
from app.services.sla import resolve_sla_due_at

logger = logging.getLogger(__name__)

# The stale-ticket sweep only re-enqueues tickets that have been pending for
# at least this long — anything younger is presumed to still be in flight
# behind its original enqueue.
_SWEEP_STALE_AFTER = timedelta(minutes=5)


async def classify_ticket(ctx: dict[str, Any], ticket_id: UUID, organization_id: UUID) -> None:
    """Classify a single ticket and persist the result.

    Opens its own session (`ctx["session_factory"]`), loads the ticket
    `for_update=True` to serialize concurrent delivery of the same job, and
    re-checks `classification_status` under that lock before doing any
    work — a duplicate job delivery (arq's at-least-once semantics) for an
    already-classified ticket is a silent no-op.

    On `ClassificationUnavailableError` (permanent failure): the ticket is
    left untouched at `classification_status="pending"` and this function
    returns normally — arq must not retry a permanent failure.

    On `ClassificationTransientError`: raises `arq.Retry` with an
    exponential backoff `defer`, so arq's own retry mechanism (bounded by
    `max_tries` on the job) retries the job later.
    """
    session_factory = ctx["session_factory"]
    embedder = ctx["embedder"]
    classify = ctx.get("classify", default_classify)

    scope = OrgScope.for_background_worker(organization_id)

    async with session_factory() as session:
        ticket_repo = TicketRepository(session, scope)
        ticket = await ticket_repo.get_or_404(ticket_id, for_update=True)

        if ticket.classification_status != ClassificationStatus.PENDING:
            # Already classified (or otherwise no longer pending) by the
            # time we acquired the lock — a duplicate delivery of this same
            # job. Nothing to do.
            return

        category_rows = (
            await session.execute(CategoryRepository(session, scope).list(active=True))
        ).scalars().all()
        urgency_rows = (
            await session.execute(UrgencyLevelRepository(session, scope).list(active=True))
        ).scalars().all()

        categories = [
            TaxonomyOption(id=row.id, name=row.name, description=row.description)
            for row in category_rows
        ]
        urgency_levels = [
            TaxonomyOption(id=row.id, name=row.name, description=row.description)
            for row in urgency_rows
        ]

        text = ticket.description or ticket.title

        try:
            result = await classify(
                text=text,
                categories=categories,
                urgency_levels=urgency_levels,
                embedder=embedder,
            )
        except ClassificationTransientError as exc:
            job_try = ctx.get("job_try", 1)
            raise Retry(defer=2**job_try) from exc
        except ClassificationUnavailableError:
            logger.warning(
                "permanent classification failure for ticket %s; leaving pending",
                ticket_id,
                exc_info=True,
            )
            return

        urgency_row = next(row for row in urgency_rows if row.id == result.urgency_id)

        ticket.category_id = result.category_id
        ticket.urgency_id = result.urgency_id
        ticket.classification_status = ClassificationStatus.CLASSIFIED
        ticket.sla_due_at = await resolve_sla_due_at(
            session,
            organization_id=organization_id,
            urgency=urgency_row,
            from_timestamp=ticket.created_at,
        )

        await ClassificationRepository(session, scope).upsert(
            ticket_id,
            predicted_category=result.predicted_category,
            confidence=result.confidence,
            predicted_urgency=result.predicted_urgency,
            urgency_confidence=result.urgency_confidence,
            model_used=result.model_used,
        )

        await session.commit()


async def sweep_pending_classifications(ctx: dict[str, Any]) -> None:
    """Re-enqueue a `classify_ticket` job for every ticket still
    `classification_status="pending"` for at least `_SWEEP_STALE_AFTER`.

    Deliberately queries `Ticket` directly rather than through the
    org-scoped `TicketRepository`: this sweep is cross-organization by
    nature (it has no single `OrgScope` to run under), so scoping it would
    require first enumerating every organization and running one query per
    org for no benefit — a background system job, not a tenant-facing
    read.
    """
    session_factory = ctx["session_factory"]
    redis = ctx["redis"]

    cutoff = datetime.now(UTC) - _SWEEP_STALE_AFTER

    async with session_factory() as session:
        stmt = select(Ticket).where(
            Ticket.classification_status == ClassificationStatus.PENDING,
            Ticket.created_at < cutoff,
        )
        stale_tickets = (await session.execute(stmt)).scalars().all()

        for ticket in stale_tickets:
            await redis.enqueue_job("classify_ticket", ticket.id, ticket.organization_id)


async def enqueue_classification(ticket_id: UUID, organization_id: UUID) -> None:
    """Enqueue a `classify_ticket` job for a just-created ticket whose
    taxonomy was omitted (`classification_status="pending"`).

    Called from the API layer via `fastapi.BackgroundTasks`, right after
    the creating request commits — see `app.api.v1.tickets.create_ticket`.
    Opens a short-lived Redis connection pool scoped to this single enqueue
    call rather than a persistent app-lifecycle pool: `app.main` has no
    lifespan hook to hang one off of yet, and a per-call pool is simple and
    cheap enough for stage 2. The pool is always closed, even if the
    enqueue itself raises.
    """
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await redis.enqueue_job("classify_ticket", ticket_id, organization_id)
    finally:
        await redis.aclose()
