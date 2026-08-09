"""`CategoryRepository` — direct-scoped (`Category` owns its own
`organization_id` column, so `scope_path` stays empty).
"""

from __future__ import annotations

from sqlalchemy import Select

from app.models.category import Category
from app.repositories.base import ScopedRepository


class CategoryRepository(ScopedRepository[Category]):
    model = Category

    def list(self, *, active: bool | None = None) -> Select[tuple[Category]]:
        """A scoped `SELECT` of categories, optionally filtered by
        `active`."""
        stmt = self.select()
        if active is not None:
            stmt = stmt.where(Category.active == active)
        return stmt
