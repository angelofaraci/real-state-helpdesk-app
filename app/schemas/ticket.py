"""Pydantic request/response schemas for the ticket endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import ClassificationStatus, TicketChannel, TicketStatus


class TicketCreate(BaseModel):
    """`property_id`/`contract_id` are deliberately non-Optional here even
    though the `tickets` table columns are nullable: the DB nullability
    exists for a hypothetical future stage (e.g. a ticket with no linked
    property), but today's business rule (Rule A) requires both on every
    ticket created through this endpoint. A missing field is rejected by
    FastAPI/Pydantic with a 422 before `ticket_service.create_ticket` ever
    runs."""

    property_id: UUID
    contract_id: UUID
    category_id: UUID
    urgency_id: UUID
    channel: TicketChannel = TicketChannel.WEB


class TicketPatch(BaseModel):
    """Only `status`/`agent_id` are patchable in stage 1 (Rule B — manual
    agent assignment, free-form status transitions)."""

    status: TicketStatus | None = None
    agent_id: UUID | None = None


class TicketResponse(BaseModel):
    id: UUID
    organization_id: UUID
    user_id: UUID
    property_id: UUID | None
    contract_id: UUID | None
    category_id: UUID
    urgency_id: UUID
    channel: TicketChannel
    status: TicketStatus
    classification_status: ClassificationStatus
    agent_id: UUID | None
    sla_due_at: datetime
    created_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}
