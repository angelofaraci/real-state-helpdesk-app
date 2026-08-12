"""`ClassificationRepository` — join-scoped (`Classification` has no
`organization_id` column of its own; scoping goes through
`tickets.organization_id`, mirroring `ContractRepository`'s join through
`properties`).

Beyond the generic `ScopedRepository` CRUD surface, this adds the two
operations the async classification worker actually needs:

- `upsert`: writes a prediction for a ticket via `INSERT ... ON CONFLICT
  (ticket_id) DO UPDATE`, so a retry/reclassify never has to fetch first to
  decide between insert and update.
- `mark_human_corrected`: flips `human_corrected` to `true` when an agent
  overrides the AI's prediction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.classification import Classification
from app.models.ticket import Ticket
from app.repositories.base import ScopedRepository


class ClassificationRepository(ScopedRepository[Classification]):
    model = Classification
    scope_path = ((Ticket, "ticket_id"),)

    async def upsert(
        self,
        ticket_id: UUID,
        *,
        predicted_category: str | None = None,
        confidence: float | None = None,
        predicted_urgency: str | None = None,
        urgency_confidence: float | None = None,
        model_used: str | None = None,
    ) -> None:
        """Insert a new `classifications` row for `ticket_id`, or update the
        existing one in place (`ticket_id` is unique) — the shape a
        classify/reclassify worker call needs without a fetch-then-branch
        round trip."""
        stmt = pg_insert(Classification).values(
            ticket_id=ticket_id,
            predicted_category=predicted_category,
            confidence=confidence,
            predicted_urgency=predicted_urgency,
            urgency_confidence=urgency_confidence,
            model_used=model_used,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticket_id"],
            set_={
                "predicted_category": stmt.excluded.predicted_category,
                "confidence": stmt.excluded.confidence,
                "predicted_urgency": stmt.excluded.predicted_urgency,
                "urgency_confidence": stmt.excluded.urgency_confidence,
                "model_used": stmt.excluded.model_used,
            },
        )
        await self._session.execute(stmt)

    async def mark_human_corrected(self, ticket_id: UUID) -> None:
        """Flag the classification for `ticket_id` as human-corrected."""
        stmt = (
            update(Classification)
            .where(Classification.ticket_id == ticket_id)
            .values(human_corrected=True)
        )
        await self._session.execute(stmt)

    async def get_by_ticket_id(self, ticket_id: UUID) -> Classification | None:
        """Fetch the `classifications` row for `ticket_id`, or `None` if the
        ticket has never been classified (by the worker or a human) — the
        lookup `ticket_service.get_ticket` uses to surface predicted
        category/urgency, confidence, and `model_used` on `GET
        /tickets/:id`."""
        stmt = self.select().where(Classification.ticket_id == ticket_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
