"""`SlaEventRepository` — direct-scoped (`SlaEvent` owns its own
`organization_id` column, so `scope_path` stays empty).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select

from app.models.sla_event import SlaEvent
from app.repositories.base import ScopedRepository


class SlaEventRepository(ScopedRepository[SlaEvent]):
    model = SlaEvent

    def list_for_ticket(self, ticket_id: UUID) -> Select[tuple[SlaEvent]]:
        """A scoped `SELECT` of SLA events for a single ticket, oldest
        first — matches `ix_sla_events_ticket_id_occurred_at`."""
        return (
            self.select()
            .where(SlaEvent.ticket_id == ticket_id)
            .order_by(SlaEvent.occurred_at)
        )
