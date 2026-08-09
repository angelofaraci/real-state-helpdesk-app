"""`PropertyRepository` — direct-scoped (`Property` owns its own
`organization_id` column, so `scope_path` stays empty).

`select()` is overridden to exclude soft-deleted rows by default: a
soft-deleted property (`deleted_at IS NOT NULL`) becomes invisible to
`get_or_404`/`update`/`delete` themselves, exactly as if it belonged to a
different organization. This is intentional for stage 1 — the one
deliberate exception (owner ticket history reaching past this default) is
out of scope for this repository and belongs to a later work unit.
"""

from __future__ import annotations

from sqlalchemy import Select

from app.models.property import Property
from app.repositories.base import ScopedRepository


class PropertyRepository(ScopedRepository[Property]):
    model = Property

    def select(self) -> Select[tuple[Property]]:
        """A scoped `SELECT` of properties, excluding soft-deleted rows."""
        return super().select().where(Property.deleted_at.is_(None))
