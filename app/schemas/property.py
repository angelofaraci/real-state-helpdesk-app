"""Pydantic request/response schemas for the properties endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import PropertyType


class PropertyCreate(BaseModel):
    owner_id: UUID
    address: str
    type: PropertyType


class PropertyUpdate(BaseModel):
    """Fields editable via `PATCH /api/v1/properties/{id}`. `owner_id` is
    deliberately not editable through this endpoint in stage 1."""

    address: str | None = None
    type: PropertyType | None = None


class PropertyResponse(BaseModel):
    id: UUID
    organization_id: UUID
    owner_id: UUID
    address: str
    type: str
    created_at: datetime

    model_config = {"from_attributes": True}
