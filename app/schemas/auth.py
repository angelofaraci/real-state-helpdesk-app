"""Pydantic request/response schemas for the auth endpoints."""

from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, EmailStr

MIN_PASSWORD_LENGTH = 12


def validate_password_length(value: str) -> str:
    """Enforce the confirmed password policy: 12 chars minimum, no forced
    complexity rules (no required uppercase/digit/symbol classes)."""
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters long")
    return value


# Reusable across any schema that accepts a plaintext password (e.g. a
# future invite-accept / registration schema introduced in a later work
# unit). Login intentionally does NOT use this type: a login password is
# verified against the stored hash, not policy-checked.
PasswordStr = Annotated[str, AfterValidator(validate_password_length)]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: UUID
    organization_id: UUID | None
    name: str
    email: str
    role: str
    status: str
