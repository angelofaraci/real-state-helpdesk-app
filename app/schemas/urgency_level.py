"""Pydantic request/response schemas for the urgency-levels endpoints.

`sla_hours > 0` is already enforced by the DB's
`ck_urgency_levels_sla_hours_positive` CHECK constraint, but it is also
validated here (`Field(gt=0)`) so an obviously-bad request fails fast with
a clean 422 instead of surfacing as an unhandled DB error.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UrgencyLevelCreate(BaseModel):
    name: str
    sla_hours: int = Field(gt=0)
    sort_order: int = 0


class UrgencyLevelUpdate(BaseModel):
    name: str | None = None
    sla_hours: int | None = Field(default=None, gt=0)
    sort_order: int | None = None
    active: bool | None = None


class UrgencyLevelResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    sla_hours: int
    sort_order: int
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UrgencyLevelDeleteResponse(BaseModel):
    """Response shape for `DELETE /api/v1/urgency-levels/{id}` — always
    HTTP 200; `deleted`/`active` distinguish a real hard-delete from the
    soft-deactivate fallback (see `app.services.taxonomy_service`)."""

    id: UUID
    deleted: bool
    active: bool

    model_config = {"from_attributes": True}
