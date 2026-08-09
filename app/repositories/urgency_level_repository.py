"""`UrgencyLevelRepository` — direct-scoped (`UrgencyLevel` owns its own
`organization_id` column, so `scope_path` stays empty).
"""

from __future__ import annotations

from sqlalchemy import Select

from app.models.urgency_level import UrgencyLevel
from app.repositories.base import ScopedRepository


class UrgencyLevelRepository(ScopedRepository[UrgencyLevel]):
    model = UrgencyLevel

    def list(self, *, active: bool | None = None) -> Select[tuple[UrgencyLevel]]:
        """A scoped `SELECT` of urgency levels, optionally filtered by
        `active`, ordered by `sort_order` (the intended display order,
        matching the `ix_urgency_levels_organization_id_active_sort_order`
        index)."""
        stmt = self.select()
        if active is not None:
            stmt = stmt.where(UrgencyLevel.active == active)
        return stmt.order_by(UrgencyLevel.sort_order)
