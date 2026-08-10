"""Pydantic response schema for the read-only ticket endpoints (Work Unit
7a). Creation/update request schemas land in Work Unit 7b."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import TicketChannel, TicketStatus


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
    agent_id: UUID | None
    sla_due_at: datetime
    created_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}
