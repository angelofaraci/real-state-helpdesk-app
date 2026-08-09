"""Pydantic request/response schemas for the contracts endpoints."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import ContractStatus


class ContractCreate(BaseModel):
    property_id: UUID
    tenant_id: UUID
    start_date: date
    end_date: date


class ContractUpdate(BaseModel):
    """Only `status` is editable via `PATCH` — start/end dates and the
    parties (`property_id`/`tenant_id`) are immutable once a contract is
    created; this stage has no amendment concept."""

    status: ContractStatus


class ContractResponse(BaseModel):
    id: UUID
    property_id: UUID
    tenant_id: UUID
    start_date: date
    end_date: date
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
