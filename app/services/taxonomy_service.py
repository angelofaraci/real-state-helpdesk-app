"""Shared delete-or-deactivate logic for taxonomy entities.

`Category` and `UrgencyLevel` are both referenced by `Ticket` through a
`RESTRICT` foreign key (`category_id`/`urgency_id`), so deleting a row that
is still referenced by a ticket must not remove it outright. This module
implements the shared "attempt a real `DELETE` inside a SAVEPOINT, fall
back to a soft `active = False` deactivation on `IntegrityError`" logic
ONCE — `category_service` and `urgency_level_service` both call
`delete_or_deactivate` instead of duplicating the SAVEPOINT dance.

The caller-facing outcome is always HTTP 200 (CONFIRMED business decision)
— never 409, even on the soft-deactivate fallback path. `TaxonomyDeleteResult`
is how the caller distinguishes which branch ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.repositories.base import ScopedRepository

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


@dataclass(frozen=True)
class TaxonomyDeleteResult:
    id: UUID
    deleted: bool
    active: bool


async def delete_or_deactivate(
    session: AsyncSession, *, repo: ScopedRepository[ModelT], id_: UUID
) -> TaxonomyDeleteResult:
    """Fetch `id_` in `repo`'s scope (raises `app.core.exceptions.NotFoundError`
    if missing/out of scope), then try to actually delete it inside a
    SAVEPOINT:

    - Nothing references the row -> the SAVEPOINT's `DELETE` succeeds, the
      row is gone: `deleted=True, active=False`.
    - A ticket references the row -> `DELETE` raises `IntegrityError`
      (the `RESTRICT` FK fires) inside the SAVEPOINT, which is rolled back;
      we then set `active = False` on the still-present row instead:
      `deleted=False, active=False`.

    Never raises for the "in use" case — always returns normally so the API
    layer can respond 200 either way.
    """
    instance = await repo.get_or_404(id_)
    try:
        async with session.begin_nested():
            await session.delete(instance)
            await session.flush()
    except IntegrityError:
        instance.active = False
        await session.flush()
        return TaxonomyDeleteResult(id=id_, deleted=False, active=False)
    return TaxonomyDeleteResult(id=id_, deleted=True, active=False)
