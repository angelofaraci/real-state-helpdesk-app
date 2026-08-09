"""Pydantic request/response schemas for the categories endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CategoryCreate(BaseModel):
    name: str
    description: str | None = None


class CategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    active: bool | None = None


class CategoryResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CategoryDeleteResponse(BaseModel):
    """Response shape for `DELETE /api/v1/categories/{id}` — always HTTP
    200; `deleted`/`active` distinguish a real hard-delete from the
    soft-deactivate fallback (see `app.services.taxonomy_service`)."""

    id: UUID
    deleted: bool
    active: bool

    model_config = {"from_attributes": True}
