"""Plain repository for `organizations`.

This is deliberately NOT a `ScopedRepository` subclass: organizations
themselves have no `organization_id` to scope by — a super-admin operates
across all of them. This is the ONE place in the codebase allowed to query
organizations without any org-scoping, and it must never be reachable
except behind `app.api.deps.require_super_admin`.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.organization import Organization


class OrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_404(self, id_: UUID) -> Organization:
        """Fetch a single organization by id, raising `NotFoundError` if it
        does not exist."""
        stmt = select(Organization).where(Organization.id == id_)
        result = await self._session.execute(stmt)
        instance = result.scalar_one_or_none()
        if instance is None:
            raise NotFoundError(Organization.__name__, id_)
        return instance

    def add(self, **fields: object) -> Organization:
        """Construct a new `Organization` and stage it on the session."""
        instance = Organization(**fields)
        self._session.add(instance)
        return instance

    async def update(self, id_: UUID, **fields: object) -> Organization:
        """Fetch an organization and apply `fields` to it."""
        instance = await self.get_or_404(id_)
        for key, value in fields.items():
            setattr(instance, key, value)
        return instance

    async def list(self) -> Sequence[Organization]:
        """Return every organization — unscoped by design."""
        stmt = select(Organization)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_by_whatsapp_phone_number_id(
        self, session: AsyncSession, phone_number_id: str
    ) -> Organization | None:
        """Resolve the organization whose WhatsApp Cloud API
        `phone_number_id` matches `phone_number_id` exactly — used by the
        inbound WhatsApp webhook to determine which org an inbound message
        belongs to. Unscoped by design, same rationale as every other
        method on this repository (see the module docstring): there is no
        `OrgScope` to run this lookup under, since resolving the org IS
        the very thing this method is for."""
        stmt = select(Organization).where(
            Organization.whatsapp_phone_number_id == phone_number_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_support_email_address(
        self, session: AsyncSession, email: str
    ) -> Organization | None:
        """Resolve the organization whose inbound `support_email_address`
        matches `email`, case-insensitively (mirroring the
        `ix_organizations_support_email_address_lower_unique` functional
        index) — used by the inbound email webhook. Unscoped, same
        rationale as `get_by_whatsapp_phone_number_id` above."""
        stmt = select(Organization).where(
            func.lower(Organization.support_email_address) == email.lower()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
