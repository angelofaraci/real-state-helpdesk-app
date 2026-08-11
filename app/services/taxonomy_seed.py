"""Default taxonomy seeding — stages the platform's default categories and
urgency levels for a just-created organization.

Called once, right after an organization is created and flushed, from
`app.services.organization_service.create_organization`. Uses
`CategoryRepository`/`UrgencyLevelRepository.add()` so every row goes
through the same org-scoping guarantees (`organization_id` forced from
`scope`) as any other write in the app — no ad-hoc row construction here.

Deliberately does NOT flush: the caller flushes once after this returns,
so all 10 rows (6 categories + 4 urgency levels) land in the same
statement batch as part of the same request-scoped transaction.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import OrgScope
from app.core.taxonomy_defaults import DEFAULT_CATEGORIES, DEFAULT_URGENCY_LEVELS
from app.repositories.category_repository import CategoryRepository
from app.repositories.urgency_level_repository import UrgencyLevelRepository


async def seed_default_taxonomy(session: AsyncSession, *, scope: OrgScope) -> None:
    """Stage the default categories and urgency levels for `scope`'s
    organization on `session`. Does not flush — the caller is responsible
    for flushing once all seeding for the request is staged."""
    category_repo = CategoryRepository(session, scope)
    for seed in DEFAULT_CATEGORIES:
        category_repo.add(name=seed.name, description=seed.description)

    urgency_repo = UrgencyLevelRepository(session, scope)
    for seed in DEFAULT_URGENCY_LEVELS:
        urgency_repo.add(name=seed.name, sla_hours=seed.sla_hours, sort_order=seed.sort_order)
