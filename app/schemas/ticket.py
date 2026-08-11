"""Pydantic request/response schemas for the ticket endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import ClassificationStatus, TicketChannel, TicketStatus


class TicketCreate(BaseModel):
    """`property_id`/`contract_id` are deliberately non-Optional here even
    though the `tickets` table columns are nullable: the DB nullability
    exists for a hypothetical future stage (e.g. a ticket with no linked
    property), but today's business rule (Rule A) requires both on every
    ticket created through this endpoint. A missing field is rejected by
    FastAPI/Pydantic with a 422 before `ticket_service.create_ticket` ever
    runs.

    `category_id`/`urgency_id` are Optional as of stage 2: classification
    now normally happens asynchronously after creation (the classification
    worker fills these in), but a caller may still supply both upfront to
    skip the async step. Supplying exactly one of the pair is a shape
    violation — Pydantic's job, not the service layer's — enforced by the
    `model_validator` below rather than left to the DB check constraint.
    """

    property_id: UUID
    contract_id: UUID
    title: str = Field(min_length=1)
    description: str | None = None
    category_id: UUID | None = None
    urgency_id: UUID | None = None
    channel: TicketChannel = TicketChannel.WEB

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _category_and_urgency_are_both_or_neither(self) -> "TicketCreate":
        if (self.category_id is None) != (self.urgency_id is None):
            raise ValueError(
                "category_id and urgency_id must both be set or both be None"
            )
        return self


class TicketPatch(BaseModel):
    """`status`/`agent_id` are patchable since stage 1 (Rule B — manual
    agent assignment, free-form status transitions). `category_id`/
    `urgency_id` become patchable as of stage 2, so an agent can correct an
    AI classification manually."""

    status: TicketStatus | None = None
    agent_id: UUID | None = None
    category_id: UUID | None = None
    urgency_id: UUID | None = None


class TicketResponse(BaseModel):
    id: UUID
    organization_id: UUID
    user_id: UUID
    property_id: UUID | None
    contract_id: UUID | None
    title: str
    description: str | None
    category_id: UUID | None
    urgency_id: UUID | None
    channel: TicketChannel
    status: TicketStatus
    classification_status: ClassificationStatus
    agent_id: UUID | None
    sla_due_at: datetime | None
    created_at: datetime
    closed_at: datetime | None

    # Classification metadata (stage 2, PR5) — populated from the ticket's
    # `classifications` row (see `ticket_service.get_ticket`), and visible
    # only to agent/admin roles: the API router nulls these out for
    # tenant/owner before returning the response (see
    # `app.api.v1.tickets.get_ticket`). Absent entirely for a ticket that
    # has never been classified (worker or human).
    predicted_category: str | None = None
    confidence: float | None = None
    predicted_urgency: str | None = None
    urgency_confidence: float | None = None
    model_used: str | None = None

    model_config = {"from_attributes": True}
