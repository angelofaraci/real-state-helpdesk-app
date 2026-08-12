"""`TicketRepository` — direct-scoped (`Ticket` owns its own
`organization_id` column, so `scope_path` stays empty) AND role-fused: unlike
every other repository so far, `select()` narrows visibility further based
on `self._scope.role`, not just `organization_id`.

Rule C (ticket visibility), CONFIRMED design:

- `agent`/`admin`: see every ticket in their organization (no extra
  restriction beyond `scope_clause()`).
- `tenant`: sees a ticket if they are its reporter (`Ticket.user_id`) OR the
  tenant on the linked contract (`contracts.tenant_id`, via
  `Ticket.contract_id`).
- `owner`: sees a ticket if they own the linked property (`properties
  .owner_id`, via `Ticket.property_id`). Deliberately does NOT exclude
  soft-deleted properties (`properties.deleted_at`) — an owner keeps seeing
  ticket history for a property they later deleted.

Fusing this into `select()` (rather than checking visibility after a
generic org-scoped fetch) is the entire point: `get_or_404` inherits it for
free, so a cross-org ticket and a same-org-but-role-invisible ticket (e.g.
one tenant fetching another tenant's ticket) 404 identically, through the
exact same code path.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, exists, or_

from app.models.contract import Contract
from app.models.enums import ClassificationStatus, TicketStatus, UserRole
from app.models.property import Property
from app.models.ticket import Ticket
from app.repositories.base import ScopedRepository


class TicketRepository(ScopedRepository[Ticket]):
    model = Ticket

    def select(self) -> Select[tuple[Ticket]]:
        """A scoped `SELECT` of tickets, further narrowed by the acting
        role's visibility rule (see module docstring)."""
        stmt = super().select()

        role = self._scope.role
        if role in (UserRole.AGENT, UserRole.ADMIN):
            return stmt

        if role == UserRole.TENANT:
            own_contract = exists().where(
                Contract.id == Ticket.contract_id,
                Contract.tenant_id == self._scope.user_id,
            )
            return stmt.where(
                or_(Ticket.user_id == self._scope.user_id, own_contract)
            )

        if role == UserRole.OWNER:
            own_property = exists().where(
                Property.id == Ticket.property_id,
                Property.owner_id == self._scope.user_id,
            )
            return stmt.where(own_property)

        # Unreachable for a well-formed OrgScope (every UserRole is one of
        # the four handled above), but fail closed rather than silently
        # returning an unscoped-by-role query if a new role is ever added.
        return stmt.where(False)  # noqa: FBT003

    def list(
        self,
        *,
        status: TicketStatus | None = None,
        category_id: UUID | None = None,
        urgency_id: UUID | None = None,
        agent_id: UUID | None = None,
        property_id: UUID | None = None,
    ) -> Select[tuple[Ticket]]:
        """A role-scoped `SELECT` of tickets, optionally filtered by the
        query params `GET /api/v1/tickets` accepts."""
        stmt = self.select()
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        if category_id is not None:
            stmt = stmt.where(Ticket.category_id == category_id)
        if urgency_id is not None:
            stmt = stmt.where(Ticket.urgency_id == urgency_id)
        if agent_id is not None:
            stmt = stmt.where(Ticket.agent_id == agent_id)
        if property_id is not None:
            stmt = stmt.where(Ticket.property_id == property_id)
        return stmt

    def list_pending(self, *, older_than: datetime) -> Select[tuple[Ticket]]:
        """A role-scoped `SELECT` of tickets still awaiting classification
        (`classification_status='pending'`) created before `older_than` —
        the query the async classification worker polls to find stale,
        unclassified tickets (e.g. after a worker crash/restart)."""
        return self.select().where(
            Ticket.classification_status == ClassificationStatus.PENDING,
            Ticket.created_at < older_than,
        )
