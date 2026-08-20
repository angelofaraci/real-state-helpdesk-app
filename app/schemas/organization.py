"""Pydantic request/response schemas for the organizations endpoints
(super-admin only)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, SecretStr


class OrganizationCreate(BaseModel):
    name: str


class OrganizationUpdate(BaseModel):
    name: str | None = None
    whatsapp_phone_number_id: str | None = None
    # Deliberately named differently from the DB column
    # (`whatsapp_access_token_encrypted`) it is encrypted into — see
    # `organization_service.update_organization`'s docstring for why this
    # mismatch is load-bearing, not an oversight. `SecretStr` also keeps
    # the raw token out of logs/reprs of this schema instance.
    whatsapp_access_token: SecretStr | None = None
    support_email_address: EmailStr | None = None


class OrganizationResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    whatsapp_phone_number_id: str | None = None
    support_email_address: str | None = None
    # Presence flag only — the token itself (plaintext or ciphertext) must
    # NEVER appear in an API response (isolation invariant I3). Backed by
    # `Organization.whatsapp_access_token_set`, a plain Python `@property`
    # picked up automatically via `from_attributes=True` below.
    whatsapp_access_token_set: bool = False

    model_config = {"from_attributes": True}
