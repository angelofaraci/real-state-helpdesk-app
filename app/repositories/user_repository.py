"""`UserRepository` — the first production `ScopedRepository` subclass.

`User` owns its own `organization_id` column, so this uses the direct
scoping strategy (`scope_path` stays empty).
"""

from __future__ import annotations

from sqlalchemy import Select

from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.repositories.base import ScopedRepository


class UserRepository(ScopedRepository[User]):
    model = User

    def list(
        self, *, role: UserRole | None = None, status: UserStatus | None = None
    ) -> Select[tuple[User]]:
        """A scoped `SELECT` of users, optionally filtered by `role` and/or
        `status`."""
        stmt = self.select()
        if role is not None:
            stmt = stmt.where(User.role == role)
        if status is not None:
            stmt = stmt.where(User.status == status)
        return stmt
