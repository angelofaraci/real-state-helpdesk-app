"""Pydantic request/response schemas for the users endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.models.enums import UserRole


class InviteUserRequest(BaseModel):
    name: str
    email: EmailStr
    role: UserRole


class UserUpdate(BaseModel):
    """Fields editable via `PATCH /api/v1/users/{id}`. Email and password
    are deliberately not editable through this endpoint in stage 1."""

    name: str | None = None
    role: UserRole | None = None


class UserResponse(BaseModel):
    id: UUID
    organization_id: UUID | None
    name: str
    email: str
    role: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
