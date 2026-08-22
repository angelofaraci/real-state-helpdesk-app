"""Pydantic request/response schemas for the organizations endpoints
(super-admin only)."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, SecretStr, field_validator

from app.services.sla import InvalidBusinessHoursError, parse_business_hours


def _check_iana_timezone(value: str) -> str:
    """Shared body for both schemas' `timezone` validator — raises a
    plain `ValueError` (pydantic wraps it into a `ValidationError`) if
    `value` is not a name `zoneinfo.ZoneInfo` can resolve."""
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"'{value}' is not a valid IANA timezone name") from exc
    return value


def _check_business_hours_schedule(value: dict) -> dict:
    """Shared body for both schemas' `business_hours` validator —
    delegates to `app.services.sla.parse_business_hours` (the SAME rules
    `compute_sla_due_at` relies on) rather than reimplementing them."""
    try:
        parse_business_hours(value)
    except InvalidBusinessHoursError as exc:
        raise ValueError(str(exc)) from exc
    return value


class OrganizationCreate(BaseModel):
    name: str
    # Stage 6 — queues + SLA (PR7a): both `None` by default, meaning "use
    # the platform default" — `organization_service.create_organization`
    # substitutes `app.core.sla_defaults.DEFAULT_TIMEZONE`/
    # `DEFAULT_BUSINESS_HOURS` explicitly when either is absent, rather
    # than relying purely on the DB `server_default` from migration 0008.
    timezone: str | None = None
    business_hours: dict | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_iana(cls, value: str | None) -> str | None:
        return _check_iana_timezone(value) if value is not None else value

    @field_validator("business_hours")
    @classmethod
    def _valid_schedule(cls, value: dict | None) -> dict | None:
        return _check_business_hours_schedule(value) if value is not None else value


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
    # Stage 6 — queues + SLA (PR7a): see `OrganizationCreate` above for the
    # validation rules; here `None` (the default) simply means "leave
    # unchanged" — same `exclude_unset=True` convention as every other
    # optional field on this schema.
    timezone: str | None = None
    business_hours: dict | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_iana(cls, value: str | None) -> str | None:
        return _check_iana_timezone(value) if value is not None else value

    @field_validator("business_hours")
    @classmethod
    def _valid_schedule(cls, value: dict | None) -> dict | None:
        return _check_business_hours_schedule(value) if value is not None else value


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
